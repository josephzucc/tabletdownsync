#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fb.py - FB Vigía (Termux)

Vigila páginas, grupos y perfiles de Facebook. La extensión de Firefox "FB Vigía"
lee las páginas (con tu sesión abierta) y envía lo que ve a este programa, que:

  - guarda un registro por fuente (página, grupo, persona) a lo largo de los días,
  - detecta qué publicaciones y comentarios son NUEVOS y los muestra primero,
  - guarda contactos, páginas, grupos, personas y miembros,
  - descarga las imágenes de las publicaciones y sigue sus enlaces,
  - lo muestra / exporta todo en texto plano (.txt).

Solo usa la biblioteca estándar de Python (no hay que instalar nada).
Uso rápido:  python fb.py ayuda
"""
import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

VERSION = '1.46'
DIR = os.environ.get('FB_DIR') or os.path.join(os.path.expanduser('~'), 'fb_datos')
DB = os.path.join(DIR, 'fb.db')
CFG = os.path.join(DIR, 'config.json')
CFG_DEF = {'puerto': 8781, 'auto_imagenes': True, 'scrolls': 8, 'max_mb': 15, 'carpeta_imagenes': '', 'navegador': '',
           'describir_imagenes': False, 'describir_max': 0, 'vision_ollama': '', 'ollama_url': 'http://127.0.0.1:11434',
           'vision_contexto': True, 'fotogramas_video': 0, 'video_segundos': 5}
UA = 'Mozilla/5.0 (Android 14; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0'
CANDADO = threading.Lock()

# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------


def ahora():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def ruta(*p):
    return os.path.join(DIR, *p)


def asegura_dirs():
    for d in ('', 'imagenes', 'enlaces', 'exportaciones', 'diagnosticos'):
        os.makedirs(ruta(d) if d else DIR, exist_ok=True)


def config():
    asegura_dirs()
    c = dict(CFG_DEF)
    if os.path.exists(CFG):
        try:
            c.update(json.load(open(CFG, encoding='utf-8')))
        except Exception:
            pass
    else:
        json.dump(c, open(CFG, 'w', encoding='utf-8'), indent=2)
    return c


def sin_acentos(s):
    return ''.join(ch for ch in unicodedata.normalize('NFD', s or '') if unicodedata.category(ch) != 'Mn')


def slug(s, n=40):
    return re.sub(r'[^a-z0-9]+', '-', sin_acentos(s).lower()).strip('-')[:n] or 'x'


def ancho():
    return max(40, min(100, shutil.get_terminal_size((80, 20)).columns))


def envolver(txt, ind='    '):
    out = []
    for par in (txt or '').split('\n'):
        par = par.strip()
        if not par:
            continue
        out.extend(textwrap.wrap(par, width=ancho(), initial_indent=ind, subsequent_indent=ind,
                                 break_long_words=True, replace_whitespace=False) or [ind.rstrip()])
    return '\n'.join(out)


def tabla(cabecera, filas):
    """Tabla de texto plano alineada (sin sintaxis markdown)."""
    filas = [[('' if c is None else sin_estilo(str(c))) for c in f] for f in filas]
    ws = [len(h) for h in cabecera]
    for f in filas:
        for i, c in enumerate(f):
            ws[i] = max(ws[i], len(c))
    ws = [min(w, 60) for w in ws[:-1]] + [ws[-1]]  # la última columna (URL, texto) nunca se corta

    def lin(f):
        return '  '.join((c if i == len(f) - 1 else c[:ws[i]]).ljust(ws[i]) for i, c in enumerate(f)).rstrip()
    out = [lin(cabecera), '  '.join('-' * w for w in ws)]
    out += [lin(f) for f in filas]
    return '\n'.join(out)


def tabla_c(cabecera, filas, **kw):
    """Igual que tabla(), con la línea «cantidad: N» al principio (N = número de filas listadas)."""
    filas = list(filas)
    return 'cantidad: %d\n\n%s' % (len(filas), tabla(cabecera, filas))


def norm_url(u):
    try:
        p = urlparse(u if '//' in u else 'https://' + u)
    except Exception:
        return u or ''
    host = p.netloc.lower()
    for pre in ('www.', 'm.', 'web.', 'mbasic.'):
        if host.startswith(pre):
            host = host[len(pre):]
    path = re.sub(r'/+$', '', p.path)
    q = parse_qs(p.query)
    keep = {k: q[k][0] for k in ('id', 'story_fbid', 'fbid', 'v', 'category', 'category_key', 'q', 'f', 'fbv_q') if k in q}
    return host + path + ('?' + urlencode(sorted(keep.items())) if keep else '')


def expandir_url(s):
    s = s.strip()
    if re.match(r'^https?://', s):
        return s
    if re.fullmatch(r'\d{6,}', s):
        return 'https://www.facebook.com/groups/' + s
    if '/' not in s and '.' not in s:
        return 'https://www.facebook.com/' + s
    return 'https://' + s


MESES = {'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6, 'julio': 7, 'agosto': 8,
         'septiembre': 9, 'setiembre': 9, 'octubre': 10, 'noviembre': 11, 'diciembre': 12}


def parse_tiempo(t, ref=None):
    """Convierte 'hace 18 horas', '3 h', 'Ayer a las 18:30', '7 de octubre' en fecha aproximada."""
    ref = ref or datetime.now()
    t = re.sub(r'[\u00a0\u202f\u034f]', ' ', (t or '')).strip().lower()
    t = re.sub(r'^(lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo),?\s+(?=\d)', '', t)   # "martes, 30 de septiembre de 2026 a las 14:03"
    if not t:
        return ''
    pm = re.search(r'\b(\d{1,2}):(\d{2})\s*(p\.?\s?m\.?|a\.?\s?m\.?)', t)
    if t in ('justo ahora', 'ahora', 'just now'):
        return ref.strftime('%Y-%m-%d %H:%M')
    m = re.match(r'^(?:hace\s+)?(\d+)\s*(segundos?|seg|s|minutos?|min|m|horas?|hrs?|h|d[ií]as?|d|semanas?|sem|w|meses|mes|a[ñn]os?|a|y)\b', t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u.startswith('sem') or u == 'w':
            d = timedelta(weeks=n)
        elif u.startswith('s'):
            d = timedelta(seconds=n)
        elif u.startswith('min') or u == 'm':
            d = timedelta(minutes=n)
        elif u.startswith('h'):
            d = timedelta(hours=n)
        elif u.startswith('d'):
            d = timedelta(days=n)
        elif u.startswith('mes'):
            d = timedelta(days=30 * n)
        else:
            d = timedelta(days=365 * n)
        return (ref - d).strftime('%Y-%m-%d %H:%M')
    hm = re.search(r'(\d{1,2}):(\d{2})', t)
    if hm and pm:   # 2:03 p. m. -> 14:03 ; 12:10 a. m. -> 00:10
        h24 = int(pm.group(1)) % 12 + (12 if pm.group(3).startswith('p') else 0)
        t = t.replace(hm.group(0), '%02d:%s' % (h24, hm.group(2)), 1)
        hm = re.search(r'(\d{1,2}):(\d{2})', t)
    base = None
    if t.startswith('ayer') or t.startswith('yesterday'):
        base = ref - timedelta(days=1)
    elif t.startswith('hoy') or t.startswith('today'):
        base = ref
    if base is not None:
        if hm:
            base = base.replace(hour=int(hm.group(1)), minute=int(hm.group(2)))
        return base.strftime('%Y-%m-%d %H:%M')
    m = re.match(r'^(\d{1,2}) de ([a-záéíóú]+)(?: de (\d{4}))?', t)
    if m and sin_acentos(m.group(2)) in MESES:
        y = int(m.group(3)) if m.group(3) else ref.year
        try:
            d = datetime(y, MESES[sin_acentos(m.group(2))], int(m.group(1)))
            if not m.group(3) and d > ref:
                d = d.replace(year=y - 1)
            if hm:
                d = d.replace(hour=int(hm.group(1)), minute=int(hm.group(2)))
            return d.strftime('%Y-%m-%d %H:%M')
        except ValueError:
            return ''
    return ''


# ----------------------------------------------------------------------------
# base de datos
# ----------------------------------------------------------------------------

ESQUEMA = """
CREATE TABLE IF NOT EXISTS fuentes(
  id INTEGER PRIMARY KEY, clave TEXT UNIQUE, tipo TEXT, nombre TEXT, url TEXT,
  creada TEXT, ultima_captura TEXT, n_capturas INTEGER DEFAULT 0, vigilada INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS fuentes_alias(clave TEXT PRIMARY KEY, fuente_id INTEGER);
CREATE TABLE IF NOT EXISTS capturas(
  id INTEGER PRIMARY KEY, fuente_id INTEGER, fecha TEXT, url TEXT,
  n_posts INTEGER, n_nuevos INTEGER, n_listas_nuevas INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS publicaciones(
  id INTEGER PRIMARY KEY, fuente_id INTEGER, clave TEXT, autor TEXT, autor_url TEXT, destino TEXT,
  tiempo_texto TEXT, fecha_aprox TEXT, texto TEXT, texto_compartido TEXT, compartido_de TEXT,
  texto_visible TEXT, visibilidad TEXT, permalink TEXT, reacciones INTEGER, ncomentarios INTEGER,
  ncompartidos INTEGER, primera_vez TEXT, ultima_vez TEXT, captura_id INTEGER, orden INTEGER,
  visto INTEGER DEFAULT 0, UNIQUE(fuente_id, clave));
CREATE TABLE IF NOT EXISTS comentarios(
  id INTEGER PRIMARY KEY, pub_id INTEGER, clave TEXT, autor TEXT, autor_url TEXT, texto TEXT,
  tiempo_texto TEXT, url TEXT, primera_vez TEXT, visto INTEGER DEFAULT 0, UNIQUE(pub_id, clave));
CREATE TABLE IF NOT EXISTS imagenes(
  id INTEGER PRIMARY KEY, pub_id INTEGER, url TEXT, norm TEXT, alt TEXT, enlace TEXT,
  archivo TEXT, estado TEXT DEFAULT 'pendiente', UNIQUE(pub_id, norm));
CREATE TABLE IF NOT EXISTS enlaces(
  id INTEGER PRIMARY KEY, pub_id INTEGER, url TEXT, norm TEXT, texto TEXT, tipo TEXT,
  estado TEXT DEFAULT 'pendiente', archivo TEXT, titulo TEXT, detalle TEXT, UNIQUE(pub_id, url));
CREATE TABLE IF NOT EXISTS listas(
  id INTEGER PRIMARY KEY, tipo TEXT, clave TEXT, ambito TEXT DEFAULT '', nombre TEXT, url TEXT,
  extra TEXT, primera_vez TEXT, ultima_vez TEXT, visto INTEGER DEFAULT 0, UNIQUE(tipo, clave, ambito));
CREATE TABLE IF NOT EXISTS mensajes(
  id INTEGER PRIMARY KEY, conv TEXT, conv_nombre TEXT, clave TEXT, autor TEXT, texto TEXT,
  primera_vez TEXT, visto INTEGER DEFAULT 0, UNIQUE(conv, clave));
CREATE TABLE IF NOT EXISTS reaccionantes(
  id INTEGER PRIMARY KEY, pub_id INTEGER, clave TEXT, nombre TEXT, url TEXT, reaccion TEXT,
  primera_vez TEXT, UNIQUE(pub_id, clave));
CREATE TABLE IF NOT EXISTS reac_comentarios(
  id INTEGER PRIMARY KEY, com_id INTEGER, clave TEXT, nombre TEXT, url TEXT, reaccion TEXT,
  primera_vez TEXT, UNIQUE(com_id, clave));
CREATE TABLE IF NOT EXISTS compartidores(
  id INTEGER PRIMARY KEY, pub_id INTEGER, clave TEXT, nombre TEXT, url TEXT,
  primera_vez TEXT, UNIQUE(pub_id, clave));
CREATE TABLE IF NOT EXISTS busquedas(
  id INTEGER PRIMARY KEY, consulta TEXT, tipo TEXT, url TEXT, creada TEXT, n_resultados INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS resultados(
  id INTEGER PRIMARY KEY, busq_id INTEGER, n INTEGER, tipo TEXT, titulo TEXT, detalle TEXT, url TEXT,
  norm TEXT, leido INTEGER DEFAULT 0, UNIQUE(busq_id, norm));
CREATE TABLE IF NOT EXISTS perfiles(
  perfil TEXT PRIMARY KEY, nombre TEXT DEFAULT '', creado TEXT);
CREATE TABLE IF NOT EXISTS avisos(
  id INTEGER PRIMARY KEY, red TEXT, cuenta TEXT, tipo TEXT, quien TEXT, quien_url TEXT, texto TEXT, extracto TEXT,
  post_url TEXT, fecha TEXT, hora TEXT, primera_vez TEXT, ultima_vez TEXT, visto INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_avisos ON avisos(red, fecha);
CREATE TABLE IF NOT EXISTS actividad(
  id INTEGER PRIMARY KEY, perfil TEXT, categoria TEXT, titulo TEXT, tipo TEXT, objetivo TEXT, fecha TEXT, hora TEXT,
  publico TEXT, texto TEXT, url TEXT, enlaces TEXT, primera_vez TEXT, ultima_vez TEXT);
CREATE INDEX IF NOT EXISTS ix_actividad ON actividad(perfil, fecha);
CREATE TABLE IF NOT EXISTS cursores(
  fuente TEXT PRIMARY KEY, clave TEXT, dn INTEGER, dia TEXT, actualizada TEXT);
CREATE TABLE IF NOT EXISTS cola(
  id INTEGER PRIMARY KEY, url TEXT, norm TEXT, scrolls INTEGER, estado TEXT, creada TEXT,
  actualizada TEXT, detalle TEXT, origen TEXT);
"""


RE_SOLO_SEGUIR = re.compile(r'^\s*(·|•|seguir|follow|siguiendo|following|a[ñn]adir como amigo|agregar a amigos|add friend)\s*$', re.I)


def sin_seguir(t):
    """Quita el botón «Seguir» que Facebook pinta junto al nombre y que se coló como texto ('Nombre · Seguir', líneas sueltas 'Seguir')."""
    if not t:
        return t
    ls = [re.sub(r'\s*[·•]\s*(Seguir|Follow)\s*$', '', l) for l in str(t).split('\n')]
    return '\n'.join(l for l in ls if not RE_SOLO_SEGUIR.match(l)).strip()


def nombre_sin_seguir(n):
    n = re.sub(r'\s*[·•]?\s*(Seguir|Follow|Siguiendo|Following)\s*$', '', n or '', flags=re.I)
    return re.sub(r'\s*[·•]\s*$', '', n).strip()   # también el «·» suelto que queda cuando el botón aún no se ha dibujado


def limpiar_seguir_db(con):
    """Limpia lo ya guardado: botones «Seguir» que quedaron como texto en comentarios y nombres."""
    n = 0
    for r in con.execute("SELECT id, autor FROM publicaciones WHERE autor LIKE '%·' OR autor LIKE '%Seguir%' OR autor LIKE '%Follow%'").fetchall():
        nv = nombre_sin_seguir(r['autor'])
        if nv != r['autor']:
            con.execute('UPDATE publicaciones SET autor=? WHERE id=?', (nv, r['id']))
            n += 1
    for tabla, col, fn in (('comentarios', 'texto', sin_seguir), ('comentarios', 'autor', nombre_sin_seguir),
                           ('reaccionantes', 'nombre', nombre_sin_seguir), ('compartidores', 'nombre', nombre_sin_seguir),
                           ('reac_comentarios', 'nombre', nombre_sin_seguir)):
        for r in con.execute("SELECT id, %s AS v FROM %s WHERE %s LIKE '%%Seguir%%' OR %s LIKE '%%Follow%%'" % (col, tabla, col, col)).fetchall():
            nv = fn(r['v'])
            if nv != r['v']:
                con.execute('UPDATE %s SET %s=? WHERE id=?' % (tabla, col), (nv, r['id']))
                n += 1
    return n


def normalizar_estilos(con):
    """Una sola vez: pasa a letra normal los nombres ya guardados con letras de adorno (fuentes y listas)."""
    try:
        if con.execute('PRAGMA user_version').fetchone()[0] >= 1:
            return
        for tab in ('fuentes', 'listas'):
            for r in con.execute('SELECT id, nombre FROM %s WHERE nombre IS NOT NULL' % tab).fetchall():
                n2 = sin_estilo(r['nombre'])
                if n2 != r['nombre']:
                    con.execute('UPDATE %s SET nombre=? WHERE id=?' % tab, (n2, r['id']))
        con.execute('PRAGMA user_version=1')
        con.commit()
    except sqlite3.Error:
        pass   # base ocupada: se intentará en la próxima orden


def migrar(con):
    """Añade columnas nuevas a bases de datos creadas con versiones anteriores."""
    for tabla, col, tipo in (('comentarios', 'respuesta', 'INTEGER DEFAULT 0'),
                             ('cola', 'accion', "TEXT DEFAULT 'capturar'"),
                             ('cola', 'pub_id', 'INTEGER'),
                             ('publicaciones', 'detalle_vez', 'TEXT'),
                             ('publicaciones', 'leida_dia', 'TEXT'),
                             ('publicaciones', 'texto_cortado', 'INTEGER DEFAULT 0'),
                             ('publicaciones', 'compartido_fecha', 'TEXT'),
                             ('publicaciones', 'compartido_cortado', 'INTEGER DEFAULT 0'),
                             ('capturas', 'cola_id', 'INTEGER'),
                             ('fuentes', 'bloqueada', 'INTEGER DEFAULT 0'),
                             ('cola', 'vigilar', 'INTEGER DEFAULT 0'),
                             ('fuentes', 'n_leer', 'INTEGER DEFAULT 0'),
                             ('cola', 'oculta', 'INTEGER DEFAULT 0'),
                             ('publicaciones', 'compartido_url', 'TEXT'),
                             ('publicaciones', 'compartido_autor_url', 'TEXT'),
                             ('publicaciones', 'original_id', 'INTEGER'),
                             ('comentarios', 'reacciones', 'INTEGER DEFAULT 0'),
                             ('publicaciones', 'detalle_limitado', 'TEXT'),
                             ('comentarios', 'lista_limitada', 'INTEGER DEFAULT 0'),
                             ('listas', 'baja', 'TEXT'),
                             ('publicaciones', 'detalle_pasada', 'INTEGER DEFAULT 0'),
                             ('publicaciones', 'detalle_hist', 'TEXT'),
                             ('imagenes', 'ocr', 'TEXT'),
                             ('imagenes', 'descripcion', 'TEXT')):
        cols = {r[1] for r in con.execute('PRAGMA table_info(%s)' % tabla).fetchall()}
        if col not in cols:
            try:
                con.execute('ALTER TABLE %s ADD COLUMN %s %s' % (tabla, col, tipo))
            except sqlite3.OperationalError:
                pass
            if (tabla, col) == ('fuentes', 'bloqueada'):   # lo desvigilado antes de esta versión queda bloqueado
                con.execute("UPDATE fuentes SET bloqueada=1 WHERE vigilada=0 AND tipo IN ('pagina','grupo','persona_grupo')")
    try:
        limpiar_seguir_db(con)
    except sqlite3.Error:
        pass
    try:
        normalizar_urls_grupos(con)
    except sqlite3.Error:
        pass
    con.commit()


RE_GRUPO_SUB = re.compile(r'^(https?://[^/]*facebook\.com/groups/[^/?#]+)/(?:members|people|about|media|files|events|photos|videos|buy_sell_discussion|learning_content|requests|admin_activities|member_requests)\b.*$', re.I)


def url_grupo_base(u):
    """https://www.facebook.com/groups/ID/members  ->  https://www.facebook.com/groups/ID/   (la página de PUBLICACIONES del grupo)."""
    if not u:
        return u
    m = RE_GRUPO_SUB.match(u.strip())
    return (m.group(1) + '/') if m else u


def normalizar_urls_grupos(con):
    """Repara las URLs de grupos guardadas como .../members (lista de miembros) en vez de la página con las publicaciones."""
    n = 0
    for tabla_ in ('listas', 'fuentes'):
        cond = "tipo='grupo'" if tabla_ == 'listas' else "url LIKE '%/groups/%'"
        for r in con.execute("SELECT id, url FROM %s WHERE %s AND url LIKE '%%/groups/%%'" % (tabla_, cond)).fetchall():
            nu = url_grupo_base(r['url'])
            if nu != r['url']:
                con.execute('UPDATE %s SET url=? WHERE id=?' % tabla_, (nu, r['id']))
                n += 1
    return n


LISTA_COMPLETA = {'lista_grupos': 'grupo', 'lista_paginas': 'pagina', 'lista_siguiendo': 'siguiendo'}


def aplicar_bajas(con, d, t0):
    """Una captura de «Tus grupos» / «Páginas que sigues» / «Siguiendo» es la lista COMPLETA de ese tipo: lo que ya no aparece
    es que dejaste de seguirlo. Se marca como baja y se deja de vigilar su fuente (para que lanzar no lo pida más).
    Salvaguarda: si la captura trae muy pocos elementos respecto a lo conocido (scroll corto, Facebook a medias) no se da de baja nada."""
    tipo = LISTA_COMPLETA.get(d.get('tipo'))
    if not tipo:
        return ''
    vistos = {(l.get('clave') or '') for l in d.get('listas') or [] if l.get('tipo') == tipo}
    vistos.discard('')
    conocidos = [r for r in con.execute("SELECT * FROM listas WHERE tipo=? AND baja IS NULL", (tipo,)).fetchall() if r['clave'] not in vistos]
    if not vistos:
        return ''
    previos = len(conocidos) + len(vistos)
    if len(vistos) < max(1, previos // 2) and len(conocidos) > 3:
        return 'lista incompleta (%d de ~%d): no se da de baja nada' % (len(vistos), previos)
    nb = 0
    for l in conocidos:
        if l['clave'] in vistos:
            continue
        con.execute('UPDATE listas SET baja=? WHERE id=?', (t0, l['id']))
        f = fuente_de_lista(con, l)
        if f and f['vigilada']:
            for i in misma_fuente(con, f):
                con.execute('UPDATE fuentes SET vigilada=0 WHERE id=?', (i,))
            cancelar_cola_de(con, misma_fuente(con, f))
        nb += 1
    return ('%d dados de baja (ya no los sigues)' % nb) if nb else ''


def reparar_capturas_cola(con):
    """Capturas pedidas desde la cola que acabaron en otra fuente (p. ej. 'otro-123') mientras la fuente pedida quedó vacía: se unen."""
    n = 0
    for c in con.execute("""SELECT c.fuente_id, k.url FROM capturas c JOIN cola k ON k.id=c.cola_id
                            WHERE COALESCE(k.accion,'capturar')='capturar' AND k.url<>''""").fetchall():
        ck = clave_de_url(c[1])
        if not ck:
            continue
        otra = fuente_por_clave(con, ck)
        act = con.execute('SELECT * FROM fuentes WHERE id=?', (c['fuente_id'],)).fetchone()
        if not otra or not act or otra['id'] == act['id'] or otra['tipo'] not in ('pagina', 'grupo'):
            continue
        vacia = (con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (otra['id'],)).fetchone()[0] == 0)
        if act['tipo'] == 'otro' or (act['tipo'] == otra['tipo'] and vacia and act['tipo'] in ('pagina', 'grupo')):
            unir_fuentes(con, otra, act)
            n += 1
    if n:
        con.commit()
    return n


def db():
    asegura_dirs()
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    migrar(con)
    normalizar_estilos(con)
    try:
        reparar_capturas_cola(con)
        reparar_nombres(con)
    except Exception:
        pass
    return con


def delta_seg(s):
    try:
        return (datetime.now() - datetime.strptime(s, '%Y-%m-%d %H:%M:%S')).total_seconds()
    except Exception:
        return 1e9


# ----------------------------------------------------------------------------
# proceso de una captura
# ----------------------------------------------------------------------------

NOMBRES_FIJOS = {'inicio': 'Inicio (Principal)', 'lista_amigos': 'Lista de amigos', 'lista_siguiendo': 'Siguiendo',
                 'lista_paginas': 'Páginas que sigo', 'lista_grupos': 'Mis grupos', 'mensajes': 'Mensajes',
                 'miembros': 'Miembros de grupo'}
TIPOS_CON_POSTS = ('inicio', 'pagina', 'grupo', 'persona_grupo', 'publicacion')


GENERICOS = {'facebook', 'inicio', 'notificaciones', 'sugerencias', 'páginas', 'paginas', 'mensajes', 'messenger',
             'watch', 'marketplace', 'grupos', 'amigos', 'home', 'log in', 'iniciar sesión', 'iniciar sesion', 'error', ''}


_PEQ_MAYUS = dict(zip('ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘʀsᴛᴜᴠᴡʏᴢ', 'ABCDEFGHIJKLMNOPRSTUVWYZ'))
_SIN_ESTILO_TAGS = ('<font>', '<wide>', '<narrow>', '<circle>', '<square>', '<isolated>', '<initial>', '<medial>', '<final>')
_INVISIBLES = dict.fromkeys(list(range(0x200B, 0x2010)) + [0x2060, 0x2061, 0x2062, 0x2063, 0xFEFF, 0x034F, 0x00AD, 0x180E])
# marcas combinadas DECORATIVAS (tachado, subrayado, recuadros): las tildes normales (U+0300..) se respetan
_DECORATIVAS = dict.fromkeys([0x0305, 0x0332, 0x0333, 0x0335, 0x0336, 0x0337, 0x0338, 0x0488, 0x0489] + list(range(0x20D0, 0x20F1)))


def sin_estilo(t):
    """Convierte las letras «de adorno» de Facebook a letras normales: negrita/cursiva/gótica/doble trazo matemática (𝐀𝐲𝐮𝐧 -> Ayun, 𝗰𝗶𝗯𝗲𝗿𝘀 -> cibers),
    anchas (ＡＢＣ), en círculo (Ⓐ), en cuadro (🄰, 🅰), versalitas (ᴀʙᴄ), cifras de adorno (𝟏) y también quita tachados/subrayados combinados y caracteres invisibles.
    No toca las tildes ni la ñ, ni símbolos como ², ½, € o los emojis."""
    if not t:
        return t
    if t.isascii():
        return t
    out = []
    for ch in t:
        o = ord(ch)
        if o in _INVISIBLES or o in _DECORATIVAS:
            continue
        if o < 0x80:
            out.append(ch)
            continue
        if ch in _PEQ_MAYUS:
            out.append(_PEQ_MAYUS[ch])
            continue
        if 0x1F130 <= o <= 0x1F149 or 0x1F150 <= o <= 0x1F169 or 0x1F170 <= o <= 0x1F189:   # letras en cuadro / círculo negro / cuadro negro
            base = (o - 0x1F130) if o <= 0x1F149 else (o - 0x1F150) if o <= 0x1F169 else (o - 0x1F170)
            out.append(chr(65 + base))
            continue
        d = unicodedata.decomposition(ch)
        if d.startswith(_SIN_ESTILO_TAGS):
            n = unicodedata.normalize('NFKC', ch)
            out.append(n)
            continue
        out.append(ch)
    return unicodedata.normalize('NFC', ''.join(out))


def limpia_nombre(n):
    """Quita '(3) ' delante y ' | Facebook' detrás. Devuelve '' si el nombre es genérico (p. ej. solo 'Facebook'). Letras de adorno -> normales."""
    n = re.sub(r'\s+', ' ', sin_estilo(n) or '').strip()
    n = re.sub(r'^\(\d+\+?\)\s*', '', n)
    n = re.sub(r'\s*[|\-–·]\s*Facebook\s*$', '', n, flags=re.I).strip()
    return '' if n.lower() in GENERICOS else n


def nombre_desde_listas(con, clave, url=''):
    """Busca en las listas capturadas (contactos, páginas, personas...) el nombre real de una fuente."""
    cands = [clave]
    if (clave or '').startswith('id-'):
        cands.append('msg-' + clave[3:])
    for c in cands:
        for r in con.execute("SELECT nombre FROM listas WHERE clave=? AND COALESCE(nombre,'')<>'' ORDER BY ultima_vez DESC", (c,)).fetchall():
            n = limpia_nombre(r['nombre'])
            if n:
                return n
    if url:
        nu = norm_url(url)
        for r in con.execute("SELECT * FROM listas WHERE COALESCE(nombre,'')<>'' AND COALESCE(url,'')<>''").fetchall():
            if norm_url(url_de_lista(r)) == nu:
                n = limpia_nombre(r['nombre'])
                if n:
                    return n
    return ''


def reparar_nombres(con, fid=None):
    """Corrige fuentes cuyo nombre quedó como 'Facebook' (o vacío / igual a la clave) usando las listas o, si no,
    el autor más frecuente de sus publicaciones (en páginas y personas). Devuelve [(id, antes, después)]."""
    q = "SELECT * FROM fuentes WHERE tipo NOT LIKE 'x\\_%' ESCAPE '\\' AND tipo NOT LIKE 'ig\\_%' ESCAPE '\\'"
    par = []
    if fid:
        q += ' AND id=?'
        par.append(fid)
    hechos = []
    for f in con.execute(q, par).fetchall():
        actual = (f['nombre'] or '').strip()
        if limpia_nombre(actual) and limpia_nombre(actual) == actual and actual.lower() != (f['clave'] or '').lower():
            continue
        if f['tipo'] in NOMBRES_FIJOS:
            continue
        n = nombre_desde_listas(con, f['clave'], f['url'])
        if not n and f['tipo'] in ('pagina', 'persona_grupo'):
            r = con.execute("""SELECT autor, COUNT(*) n FROM publicaciones WHERE fuente_id=? AND COALESCE(autor,'')<>''
                               GROUP BY autor ORDER BY n DESC LIMIT 1""", (f['id'],)).fetchone()
            if r and limpia_nombre(r['autor']):
                n = limpia_nombre(r['autor'])
        if n and n != actual:
            con.execute('UPDATE fuentes SET nombre=? WHERE id=?', (n, f['id']))
            hechos.append((f['id'], actual, n))
    if hechos:
        con.commit()
    return hechos


def norm_nombre(n):
    """Nombre en minúsculas, sin acentos ni signos, para comparar: 'La Adrada Existe 👍' == 'la adrada existe'."""
    import unicodedata
    t = unicodedata.normalize('NFD', (n or '').lower())
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', ' ', t).strip()


def familia(tipo):
    return 'grupo' if tipo == 'grupo' else 'pagina' if tipo == 'pagina' else tipo


def clave_de_url(u):
    """Clave de fuente que la extensión asignaría a esa URL (páginas, perfiles y grupos)."""
    try:
        p = urlparse(u)
    except Exception:
        return ''
    path = re.sub(r'/+$', '', p.path)
    q = parse_qs(p.query)
    m = re.match(r'^/groups/([^/]+)(?:/(?:members|people|about|search|media|files|events|photos|videos))?$', path)
    if m and m.group(1) not in ('feed', 'joins', 'discover'):
        return 'grupo-' + m.group(1)
    if path == '/profile.php' and q.get('id'):
        return 'id-' + q['id'][0]
    m = re.match(r'^/pages/[^/]+/(\d+)$', path)
    if m:
        return 'id-' + m.group(1)   # páginas de lugares/ciudades: facebook.com/pages/La%20Adrada,%20Ávila/110333458996497
    m = re.match(r'^/([^/]+)$', path)
    if m:
        return m.group(1).lower()
    return ''


def misma_fuente(con, f):
    """Ids de fuentes que son LA MISMA página/grupo/persona que f (misma URL, alias o mismo nombre y tipo), incluida f."""
    ids = {f['id']}
    nu = norm_url(f['url']) if f['url'] else ''
    nn = norm_nombre(f['nombre'])
    for g in con.execute('SELECT * FROM fuentes WHERE id<>?', (f['id'],)).fetchall():
        if f['tipo'].startswith(('x_', 'ig_')) or g['tipo'].startswith(('x_', 'ig_')):
            continue
        if familia(g['tipo']) != familia(f['tipo']) or g['tipo'] in ('inicio', 'mensajes', 'publicacion'):
            continue
        if nu and g['url'] and norm_url(g['url']) == nu:
            ids.add(g['id'])
        elif nn and len(nn) > 3 and norm_nombre(g['nombre']) == nn and norm_nombre(g['nombre']) != norm_nombre(g['clave']):
            ids.add(g['id'])
    for a in con.execute('SELECT clave, fuente_id FROM fuentes_alias').fetchall():
        if a['fuente_id'] == f['id']:
            g = con.execute('SELECT id FROM fuentes WHERE clave=?', (a['clave'],)).fetchone()
            if g:
                ids.add(g['id'])
    return ids


def cancelar_cola_de(con, f_ids):
    """Anula las órdenes PENDIENTES de la cola que apuntan a esas fuentes (por URL)."""
    urls = set()
    for i in f_ids:
        r = con.execute('SELECT url FROM fuentes WHERE id=?', (i,)).fetchone()
        if r and r['url']:
            urls.add(norm_url(r['url']))
    n = 0
    for c in con.execute("SELECT id, norm FROM cola WHERE estado='pendiente' AND COALESCE(accion,'capturar')='capturar'").fetchall():
        if c['norm'] in urls:
            con.execute("UPDATE cola SET estado='cancelada', detalle='fuente desvigilada', actualizada=? WHERE id=?", (ahora(), c['id']))
            n += 1
    return n


def poner_vigilada(con, f, valor):
    """Vigila (1) o deja de vigilar (0) una fuente Y sus duplicadas; deja constancia de que lo decidió el usuario."""
    ids = misma_fuente(con, f)
    for i in ids:
        con.execute('UPDATE fuentes SET vigilada=?, bloqueada=? WHERE id=?', (valor, 0 if valor else 1, i))
    canceladas = cancelar_cola_de(con, ids) if not valor else 0
    return ids, canceladas


def bloqueada_igual(con, tipo, clave, nombre, url):
    """¿Hay una fuente que el usuario desvigiló y que es esta misma (por URL, alias o nombre)?"""
    nu = norm_url(url) if url else ''
    nn = norm_nombre(nombre)
    for g in con.execute('SELECT * FROM fuentes WHERE bloqueada=1').fetchall():
        if familia(g['tipo']) != familia(tipo):
            continue
        if nu and g['url'] and norm_url(g['url']) == nu:
            return True
        if nn and len(nn) > 3 and norm_nombre(g['nombre']) == nn and nn != norm_nombre(clave):
            return True
    return False


def fuente_por_clave(con, clave):
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()
    if f:
        return f
    a = con.execute('SELECT fuente_id FROM fuentes_alias WHERE clave=?', (clave,)).fetchone()
    return con.execute('SELECT * FROM fuentes WHERE id=?', (a['fuente_id'],)).fetchone() if a else None


def pagina_de_post_url(u):
    """De la URL de una publicación saca (clave, tipo, url de la página/grupo, id de la publicación); None si no se reconoce."""
    try:
        p = urlparse(u)
    except Exception:
        return None
    if not re.search(r'(^|\.)facebook\.com$', p.hostname or ''):
        return None
    path = p.path
    q = parse_qs(p.query)
    m = re.match(r'^/groups/([^/]+)/(?:posts|permalink)/([^/?]+)', path)
    if m:
        return {'clave': 'grupo-' + m.group(1), 'tipo': 'grupo', 'url': 'https://www.facebook.com/groups/%s' % m.group(1), 'id': m.group(2)}
    m = re.match(r'^/([^/]+)/(?:posts|videos|reel)/([^/?]+)', path)
    if m and m.group(1) not in ('groups', 'watch', 'reel', 'photo', 'permalink.php'):
        return {'clave': m.group(1).lower(), 'tipo': 'pagina', 'url': 'https://www.facebook.com/%s' % m.group(1), 'id': m.group(2)}
    mg = re.match(r'^gm\.(\d+)$', (q.get('set') or [''])[0])
    if path.startswith('/photo') and mg and (q.get('idorvanity') or [''])[0].isdigit():   # foto de un grupo: set=gm.NÚMERO_DE_LA_PUBLICACIÓN
        g_ = q['idorvanity'][0]
        return {'clave': 'grupo-' + g_, 'tipo': 'grupo', 'url': 'https://www.facebook.com/groups/%s' % g_, 'id': mg.group(1)}
    if path in ('/permalink.php', '/story.php') and q.get('id') and q.get('story_fbid'):
        return {'clave': 'id-' + q['id'][0], 'tipo': 'pagina', 'url': 'https://www.facebook.com/profile.php?id=%s' % q['id'][0], 'id': q['story_fbid'][0]}
    return None


def fuente_de_pagina(con, pg):
    """La fuente (página/grupo) de una publicación; si no existía, se crea (sin vigilar)."""
    f = fuente_por_clave(con, pg['clave'])
    if f:
        return f['id']
    cur = con.execute('INSERT INTO fuentes(clave,tipo,nombre,url,creada,vigilada,bloqueada) VALUES(?,?,?,?,?,0,0)',
                      (pg['clave'], pg['tipo'], pg['clave'], pg['url'], ahora()))
    return cur.lastrowid


def unir_fuentes(con, destino, origen):
    """Mueve todo lo de 'origen' a 'destino' y borra 'origen'; la clave antigua queda como alias."""
    if destino['id'] == origen['id']:
        return
    for p in con.execute('SELECT id, clave FROM publicaciones WHERE fuente_id=?', (origen['id'],)).fetchall():
        if con.execute('SELECT 1 FROM publicaciones WHERE fuente_id=? AND clave=?', (destino['id'], p['clave'])).fetchone():
            for t in ('comentarios', 'imagenes', 'enlaces', 'reaccionantes', 'compartidores'):
                con.execute('DELETE FROM %s WHERE pub_id=?' % t, (p['id'],))
            con.execute('DELETE FROM publicaciones WHERE id=?', (p['id'],))
        else:
            con.execute('UPDATE publicaciones SET fuente_id=? WHERE id=?', (destino['id'], p['id']))
    con.execute('UPDATE capturas SET fuente_id=? WHERE fuente_id=?', (destino['id'], origen['id']))
    con.execute('UPDATE fuentes_alias SET fuente_id=? WHERE fuente_id=?', (destino['id'], origen['id']))
    con.execute('INSERT OR REPLACE INTO fuentes_alias(clave, fuente_id) VALUES(?,?)', (origen['clave'], destino['id']))
    bloq = 1 if (destino['bloqueada'] or origen['bloqueada']) else 0
    vig = 0 if bloq else max(destino['vigilada'], origen['vigilada'])
    con.execute('UPDATE fuentes SET n_capturas=n_capturas+?, bloqueada=?, vigilada=?, ultima_captura=MAX(COALESCE(ultima_captura,\'\'),?) WHERE id=?',
                (origen['n_capturas'] or 0, bloq, vig, origen['ultima_captura'] or '', destino['id']))
    con.execute('DELETE FROM fuentes WHERE id=?', (origen['id'],))


def url_sin_busqueda(u):
    """La URL de una BÚSQUEDA dentro de un grupo/página (.../search?q=...) no es la dirección de la fuente: se deja la del grupo/página."""
    m = re.match(r'^(https?://[^/]*facebook\.com/(?:groups/[^/?#]+|profile/\d+|[^/?#]+))/search(?:/|\?|$)', u or '', re.I)
    if m:
        b = m.group(1)
        return b + '/' if '/groups/' in b else b
    return u

def fuente_de(con, d):
    tipo = d.get('tipo') or 'otro'
    clave = d.get('clave') or 'otro'
    propio = not tipo.startswith(('x_', 'ig_'))
    nombre = limpia_nombre(d.get('nombre')) if propio else (d.get('nombre') or '').strip()
    if tipo in NOMBRES_FIJOS and tipo != 'miembros':
        nombre = NOMBRES_FIJOS[tipo]
    url = url_sin_busqueda(d.get('url') or '')
    if not nombre and propio:
        nombre = nombre_desde_listas(con, clave, url)
    f = fuente_por_clave(con, clave)
    if f:
        if nombre:
            con.execute('UPDATE fuentes SET nombre=?, url=? WHERE id=?', (nombre, url or f['url'], f['id']))
        return f['id']
    if not nombre:
        nombre = (d.get('nombre') or '').strip() or clave
        if propio and nombre.lower() == 'facebook':
            nombre = clave   # provisional: se corrige solo con las listas o los autores (reparar_nombres)
    # Facebook: una fuente solo entra en "vigilados" cuando TÚ lo pides (vigilar, sugerir --anadir...). X e Instagram: sus cuentas, como siempre.
    vig = 1 if tipo in ('x_cuenta', 'ig_cuenta') else 0
    bloq = 0
    if vig and propio and bloqueada_igual(con, tipo, clave, nombre, url):
        vig, bloq = 0, 1   # es una página/grupo que el usuario desvigiló: no se vuelve a vigilar sola
    cur = con.execute('INSERT INTO fuentes(clave,tipo,nombre,url,creada,vigilada,bloqueada) VALUES(?,?,?,?,?,?,?)',
                      (clave, tipo, nombre, url, ahora(), vig, bloq))
    nuevo = cur.lastrowid
    # si la URL que se pidió (cola) tenía otra clave que la página final (vanity <-> id), es la misma fuente
    return nuevo


def _txt_cmp(t):
    """Texto de una publicación normalizado para compararlo (sin 'Ver más/menos', puntos suspensivos, <>, mayúsculas ni espacios de más)."""
    t = re.sub(r'<([^<>\n]{1,80})>', r'\1', t or '')
    t = re.sub(r'(?i)\b(ver m[aá]s|ver menos|see more|see less)\b', ' ', t)
    t = t.replace('\u2026', ' ').replace('...', ' ')
    return re.sub(r'\W+', ' ', sin_acentos(t.lower())).strip()


def mismo_texto(a, b):
    """¿Son la misma publicación una leída recortada ('Ver más') y otra completa? Una empieza igual que la otra."""
    a, b = _txt_cmp(a), _txt_cmp(b)
    n = min(len(a), len(b))
    if n < 25:
        return a == b and n >= 8
    n = n - 6   # margen: el recorte puede cortar a media palabra
    return a[:n] == b[:n]


RE_IDS_URL = [r'/posts/(\d{6,}|pfbid\w+)', r'/permalink/(\d{6,})', r'/reels?/(\d{6,})', r'/videos/(?:pcb\.\d+/)?(\d{6,})', r'/videos/pcb\.(\d{6,})',
              r'[?&](?:v|fbid|story_fbid)=(\d{6,})', r'/(pfbid\w{20,})']


def ids_url(u):
    """Identificadores concretos (publicación, vídeo, reel, foto) que lleva una dirección de Facebook."""
    out = set()
    for rx in RE_IDS_URL:
        for m in re.finditer(rx, u or ''):
            out.add('id:' + m.group(1))
    return out


def id_imagen(u):
    m = re.search(r'/([\w-]+_n)\.\w+', (u or '').split('?')[0])
    return ('im:' + m.group(1)) if m else ''


def huella_p(p):
    """Todo lo que identifica una publicación recién leída (dict de la extensión): enlaces propios, vídeos/reels que lleva e imágenes."""
    fid, im = set(), set()
    fid |= ids_url(p.get('permalink'))
    fid |= ids_url(p.get('compartido_url'))
    for e in p.get('enlaces') or []:
        fid |= ids_url(e.get('url'))
    for i in p.get('imagenes') or []:
        k = id_imagen(i.get('src'))
        if k:
            im.add(k)
    return fid, im


def huella_db(con, x):
    fid = ids_url(x['permalink'])
    for r in con.execute('SELECT url FROM enlaces WHERE pub_id=?', (x['id'],)).fetchall():
        fid |= ids_url(r['url'])
    im = set()
    for r in con.execute('SELECT url FROM imagenes WHERE pub_id=?', (x['id'],)).fetchall():
        k = id_imagen(r['url'])
        if k:
            im.add(k)
    return fid, im


def misma_pub(con, x, p, dia, aut, perm_p, huella):
    """¿La publicación guardada x (fila) es la misma que p (dict leído ahora)? Por orden de fiabilidad:
    1) mismo enlace; 2) mismo autor y comparten algún identificador (publicación, vídeo, reel, foto) o la misma imagen;
    3) mismo autor, mismo día y texto igual o uno recortado."""
    if perm_p and x['permalink'] and norm_url(x['permalink']) == perm_p:
        return True
    if nombre_sin_seguir(x['autor']) != aut:
        return False
    dx = (x['fecha_aprox'] or '')[:10]
    if dia and dx and dia != dx:
        return False
    fp, ip = huella
    if fp or ip:
        fx, ix = huella_db(con, x)
        if fp & fx or ip & ix:
            return True
    return bool((p.get('texto') or '').strip() and (x['texto'] or '').strip() and mismo_texto(p.get('texto'), x['texto']))


def buscar_gemela(con, fid, p, fecha):
    """Publicación YA guardada en la misma página/grupo que es la misma que 'p' aunque su clave sea distinta
    (leída antes recortada por el 'Ver más', sin texto o sin enlace aún, con otra foto como enlace, con «· Seguir» en el nombre). None si no hay."""
    aut = nombre_sin_seguir(p.get('autor'))
    dia = (fecha or '')[:10]
    perm = norm_url(p.get('permalink') or '') if p.get('permalink') else ''
    huella = huella_p(p)
    for x in con.execute('SELECT id, autor, texto, fecha_aprox, permalink FROM publicaciones WHERE fuente_id=? ORDER BY id DESC LIMIT 600', (fid,)).fetchall():
        if misma_pub(con, x, p, dia, aut, perm, huella):
            return x['id']
    return None


def mejor_permalink(a, b):
    """Entre dos enlaces de la misma publicación, el de la publicación (posts/pfbid) mejor que el de una foto."""
    def pts(u):
        return 2 if re.search(r'/(posts|permalink|videos)/|pfbid|story_fbid', u or '') else (1 if u else 0)
    return a if pts(a) >= pts(b) else b


def guardar_comentarios_detalle(con, pid, comentarios, t0):
    """Guarda los comentarios leídos por 'detalle' (los nuevos; en los que ya había, el texto más completo y las reacciones). Devuelve cuántos son nuevos."""
    nuevos = 0
    for c in comentarios:
        ck = c.get('clave') or hashlib.sha1(((c.get('autor') or '') + (c.get('texto') or '')).encode()).hexdigest()[:16]
        c['texto'] = sin_seguir(c.get('texto'))
        c['autor'] = nombre_sin_seguir(c.get('autor'))
        prev = con.execute('SELECT id, texto FROM comentarios WHERE pub_id=? AND clave=?', (pid, ck)).fetchone()
        nre = int(c.get('reacciones') or 0)
        if not prev:
            con.execute('INSERT INTO comentarios(pub_id,clave,autor,autor_url,texto,tiempo_texto,url,primera_vez,respuesta,reacciones) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (pid, ck, c.get('autor'), c.get('autor_url'), c.get('texto'), c.get('tiempo_texto'), c.get('url'), t0, 1 if c.get('respuesta') else 0, nre))
            nuevos += 1
        else:
            if len(c.get('texto') or '') > len(prev['texto'] or ''):
                con.execute('UPDATE comentarios SET texto=? WHERE id=?', (c.get('texto'), prev['id']))
            if nre:
                con.execute('UPDATE comentarios SET reacciones=? WHERE id=?', (nre, prev['id']))
    return nuevos


def procesar_captura(con, d):
    t0 = ahora()
    if d.get('tipo') == 'actividad':
        return procesar_actividad(con, d)
    av_res = None
    if d.get('tipo') in ('x_avisos', 'ig_avisos'):
        av_res, _ = procesar_avisos(con, d)
        if not d.get('posts'):
            return av_res, None
    fid = None
    if d.get('cola_id') and d.get('tipo') in ('pagina', 'grupo', 'otro', 'persona_grupo', None):
        # La URL que TÚ pediste manda: si Facebook te llevó a una dirección que no se reconoce (p. ej. /people/Nombre/123,
        # o una vanidad distinta), lo leído se guarda igualmente en la fuente de la URL pedida.
        cr0 = con.execute('SELECT url FROM cola WHERE id=?', (d['cola_id'],)).fetchone()
        ck0 = clave_de_url(cr0['url']) if cr0 and cr0['url'] else ''
        f0 = fuente_por_clave(con, ck0) if ck0 else None
        if f0 and f0['tipo'] in ('pagina', 'grupo') and (d.get('tipo') in ('otro', None) or str(d.get('clave') or '').startswith('otro')):
            fid = f0['id']
            if d.get('clave'):
                con.execute('INSERT OR REPLACE INTO fuentes_alias(clave, fuente_id) VALUES(?,?)', (d['clave'], fid))
            nom = limpia_nombre(d.get('nombre'))
            if nom and (f0['nombre'] == f0['clave'] or not f0['nombre']):
                con.execute('UPDATE fuentes SET nombre=? WHERE id=?', (nom, fid))
    if fid is None and d.get('cola_id') and d.get('tipo') == 'publicacion':
        cr1 = con.execute('SELECT url FROM cola WHERE id=?', (d['cola_id'],)).fetchone()
        pg = pagina_de_post_url(cr1['url']) if cr1 and cr1['url'] else None
        if pg:   # la publicación se guarda en SU página o grupo (p. ej. Diario de Ávila), no en una fuente suelta "post-123"
            fid = fuente_de_pagina(con, pg)
    if fid is None:
        fid = fuente_de(con, d)
    if d.get('cola_id') and d.get('tipo') in ('pagina', 'grupo'):
        cr = con.execute('SELECT url FROM cola WHERE id=?', (d['cola_id'],)).fetchone()
        ck = clave_de_url(cr['url']) if cr and cr['url'] else ''
        if ck and ck != d.get('clave'):
            otra = fuente_por_clave(con, ck)
            actual = con.execute('SELECT * FROM fuentes WHERE id=?', (fid,)).fetchone()
            if otra and otra['id'] != fid:   # la misma página existía con otra clave: se unen
                menor, mayor = (otra, actual) if otra['id'] < actual['id'] else (actual, otra)
                unir_fuentes(con, menor, mayor)
                fid = menor['id']
            elif not otra:
                con.execute('INSERT OR REPLACE INTO fuentes_alias(clave, fuente_id) VALUES(?,?)', (ck, fid))
    if d.get('cola_id'):
        cv = con.execute('SELECT vigilar FROM cola WHERE id=?', (d['cola_id'],)).fetchone()
        if cv and cv['vigilar'] and d.get('tipo') in ('pagina', 'grupo', 'persona_grupo'):
            con.execute('UPDATE fuentes SET vigilada=1, bloqueada=0 WHERE id=?', (fid,))
    posts = d.get('posts') or []
    cur = con.execute('INSERT INTO capturas(fuente_id,fecha,url,n_posts,n_nuevos,cola_id) VALUES(?,?,?,?,0,?)',
                      (fid, t0, d.get('url', ''), len(posts), d.get('cola_id')))
    cap = cur.lastrowid
    nuevos = com_nuevos = 0
    ref = datetime.now()
    # Facebook pone a TODAS las tarjetas el enlace de la propia página mientras no se pasa el ratón por su hora: si varias publicaciones
    # de la misma captura traen el mismo enlace, solo vale para una (la que tiene hora); a las demás se les quita (si no, se fundirían en una)
    _perm = {}
    for p_ in posts:
        if p_.get('permalink') and not RE_REEL.search(p_['permalink']):
            _perm.setdefault(p_['permalink'], []).append(p_)
    for u_, lst_ in _perm.items():
        if len(lst_) > 1:
            keep_ = next((x for x in lst_ if x.get('tiempo_texto')), lst_[0])
            for x in lst_:
                if x is not keep_:
                    x['permalink'] = ''
                    x['post_id'] = ''
    for orden, p in enumerate(posts):
        if p.get('autor'):
            p['autor'] = nombre_sin_seguir(p['autor'])
        clave = p.get('clave') or hashlib.sha1((p.get('autor', '') + p.get('texto', '')).encode()).hexdigest()[:16]
        fecha = p.get('fecha_abs') or parse_tiempo(p.get('tiempo_texto'), ref)
        f = con.execute('SELECT id FROM publicaciones WHERE fuente_id=? AND clave=?', (fid, clave)).fetchone()
        if not f and not str(d.get('tipo') or '').startswith(('x_', 'ig_')):
            gid = buscar_gemela(con, fid, p, fecha)   # misma publicación leída antes recortada o con otro enlace: no se duplica
            if gid:
                f = {'id': gid}
                g = con.execute('SELECT permalink FROM publicaciones WHERE id=?', (gid,)).fetchone()
                con.execute('UPDATE publicaciones SET permalink=? WHERE id=?', (mejor_permalink(g['permalink'] or '', p.get('permalink') or ''), gid))
        if not f:
            cur = con.execute(
                """INSERT INTO publicaciones(fuente_id,clave,autor,autor_url,destino,tiempo_texto,fecha_aprox,texto,
                   texto_compartido,compartido_de,texto_visible,visibilidad,permalink,reacciones,ncomentarios,
                   ncompartidos,primera_vez,ultima_vez,captura_id,orden,visto,texto_cortado)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (fid, clave, p.get('autor'), p.get('autor_url'), p.get('destino'), p.get('tiempo_texto'), fecha,
                 p.get('texto'), p.get('texto_compartido'), p.get('compartido_de'), p.get('texto_visible'),
                 p.get('visibilidad'), p.get('permalink'), p.get('reacciones'), p.get('ncomentarios'),
                 p.get('ncompartidos'), t0, t0, cap, orden, 1 if (p.get('truncado') and (p.get('texto') or '').strip()) else 0))
            pid = cur.lastrowid
            nuevos += 1
        else:
            pid = f['id']
            # la hora absoluta se fija la primera vez: no se pisa con la relativa de hoy
            con.execute(
                """UPDATE publicaciones SET ultima_vez=?,
                   reacciones=COALESCE(?,reacciones), ncomentarios=COALESCE(?,ncomentarios),
                   ncompartidos=COALESCE(?,ncompartidos),
                   permalink=CASE WHEN COALESCE(permalink,'')='' THEN ? ELSE permalink END,
                   tiempo_texto=CASE WHEN COALESCE(fecha_aprox,'')='' AND ?<>'' THEN ? ELSE tiempo_texto END,
                   fecha_aprox=CASE WHEN COALESCE(fecha_aprox,'')='' THEN ? ELSE fecha_aprox END,
                   texto_cortado=CASE WHEN length(COALESCE(?,''))>length(COALESCE(texto,'')) THEN ?
                                      WHEN ?=0 AND length(COALESCE(?,''))>=length(COALESCE(texto,'')) THEN 0 ELSE texto_cortado END,
                   texto=CASE WHEN length(COALESCE(?,''))>length(COALESCE(texto,'')) THEN ? ELSE texto END
                   WHERE id=?""",
                (t0, p.get('reacciones'), p.get('ncomentarios'), p.get('ncompartidos'),
                 p.get('permalink') or '', (fecha and p.get('tiempo_texto')) or '', p.get('tiempo_texto') or '', fecha or '',
                 p.get('texto'), 1 if p.get('truncado') else 0, 1 if p.get('truncado') else 0, p.get('texto'),
                 p.get('texto'), p.get('texto'), pid))
        if p.get('compartido_fecha') or p.get('truncado_reel'):
            con.execute("UPDATE publicaciones SET compartido_fecha=COALESCE(NULLIF(?,''),compartido_fecha), compartido_cortado=CASE WHEN ? THEN 1 ELSE compartido_cortado END WHERE id=?",
                        (p.get('compartido_fecha') or '', 1 if p.get('truncado_reel') else 0, pid))
        if p.get('compartido_url') or p.get('compartido_autor_url'):
            con.execute("UPDATE publicaciones SET compartido_url=COALESCE(NULLIF(?,''),compartido_url), compartido_autor_url=COALESCE(NULLIF(?,''),compartido_autor_url) WHERE id=?",
                        (p.get('compartido_url') or '', p.get('compartido_autor_url') or '', pid))
        if p.get('completa') and not p.get('truncado'):   # (con el texto cortado no vale: se relee) la extensión la leyó a fondo (comentarios incluidos): no se repite hasta mañana... si tiene fecha y contenido
            fila_ = con.execute('SELECT texto, texto_compartido, fecha_aprox, tiempo_texto, texto_cortado, (SELECT COUNT(*) FROM imagenes WHERE pub_id=publicaciones.id) ni FROM publicaciones WHERE id=?', (pid,)).fetchone()
            if fila_ and not fila_['texto_cortado'] and (fila_['fecha_aprox'] or fila_['tiempo_texto']) and ((fila_['texto'] or '').strip() or (fila_['texto_compartido'] or '').strip() or fila_['ni'] or p.get('imagenes')
                                    or any(re.search(r'/(reels?|videos|watch|photo)\b', e.get('url') or '') for e in (p.get('enlaces') or []))):
                con.execute('UPDATE publicaciones SET leida_dia=? WHERE id=?', (datetime.now().strftime('%Y-%m-%d'), pid))
        for c in p.get('comentarios') or []:
            ck = c.get('clave') or hashlib.sha1(((c.get('autor') or '') + (c.get('texto') or '')).encode()).hexdigest()[:16]
            c['texto'] = sin_seguir(c.get('texto'))
            c['autor'] = nombre_sin_seguir(c.get('autor'))
            prev = con.execute('SELECT id, texto FROM comentarios WHERE pub_id=? AND clave=?', (pid, ck)).fetchone()
            nre = int(c.get('reacciones') or 0)
            if not prev:
                con.execute('INSERT INTO comentarios(pub_id,clave,autor,autor_url,texto,tiempo_texto,url,primera_vez,respuesta,reacciones) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (pid, ck, c.get('autor'), c.get('autor_url'), c.get('texto'), c.get('tiempo_texto'), c.get('url'), t0, 1 if c.get('respuesta') else 0, nre))
                com_nuevos += 1
            else:
                if len(c.get('texto') or '') > len(prev['texto'] or ''):  # el comentario se leyó ahora completo
                    con.execute('UPDATE comentarios SET texto=? WHERE id=?', (c.get('texto'), prev['id']))
                if nre:
                    con.execute('UPDATE comentarios SET reacciones=? WHERE id=?', (nre, prev['id']))
        for im in p.get('imagenes') or []:
            src = im.get('src') or ''
            if not src:
                continue
            n = src if src.startswith('data:') else src.split('?')[0]
            if src.startswith('data:'):
                n = 'data:' + hashlib.sha1(src.encode()).hexdigest()
            con.execute('INSERT OR IGNORE INTO imagenes(pub_id,url,norm,alt,enlace) VALUES(?,?,?,?,?)',
                        (pid, src, n, im.get('alt'), im.get('enlace')))
        for e in p.get('enlaces') or []:
            u = e.get('url') or ''
            if u:
                con.execute('INSERT OR IGNORE INTO enlaces(pub_id,url,norm,texto,tipo) VALUES(?,?,?,?,?)',
                            (pid, u, norm_url(u), e.get('texto'), e.get('tipo') or 'externo'))
    con.execute('UPDATE capturas SET n_nuevos=? WHERE id=?', (nuevos, cap))

    # listas (contactos, páginas, grupos, personas, miembros, conversaciones)
    ln = 0
    for l in d.get('listas') or []:
        if not l.get('clave'):
            continue
        amb = l.get('ambito') or ''
        l['nombre'] = sin_estilo(l.get('nombre'))
        if l.get('tipo') == 'grupo':
            l['url'] = url_grupo_base(l.get('url') or '')
        f = con.execute('SELECT id FROM listas WHERE tipo=? AND clave=? AND ambito=?', (l['tipo'], l['clave'], amb)).fetchone()
        if f:
            con.execute('UPDATE listas SET baja=NULL, ultima_vez=?, nombre=COALESCE(NULLIF(?,\'\'),nombre), url=COALESCE(NULLIF(?,\'\'),url), extra=COALESCE(NULLIF(?,\'\'),extra) WHERE id=?',
                        (t0, l.get('nombre'), l.get('url'), l.get('extra'), f['id']))
        else:
            con.execute('INSERT INTO listas(tipo,clave,ambito,nombre,url,extra,primera_vez,ultima_vez) VALUES(?,?,?,?,?,?,?,?)',
                        (l['tipo'], l['clave'], amb, l.get('nombre'), l.get('url'), l.get('extra'), t0, t0))
            ln += 1
    bajas_txt = aplicar_bajas(con, d, t0)
    mn = 0
    for m in d.get('mensajes') or []:
        if not con.execute('SELECT 1 FROM mensajes WHERE conv=? AND clave=?', (m['conv'], m['clave'])).fetchone():
            con.execute('INSERT INTO mensajes(conv,conv_nombre,clave,autor,texto,primera_vez) VALUES(?,?,?,?,?,?)',
                        (m['conv'], m.get('conv_nombre'), m['clave'], m.get('autor'), m.get('texto'), t0))
            mn += 1
    con.execute('UPDATE capturas SET n_listas_nuevas=? WHERE id=?', (ln + mn, cap))
    con.execute('UPDATE fuentes SET ultima_captura=?, n_capturas=n_capturas+1 WHERE id=?', (t0, fid))
    # enlaces de la cola de "seguir" resueltos por esta captura
    con.execute("UPDATE enlaces SET estado='hecho', detalle='capturado con la extensión' WHERE estado='cola' AND norm=?",
                (norm_url(d.get('url', '')),))
    foto = None
    if d.get('foto') and d['foto'].get('src'):
        foto = d['foto']
    if ln or d.get('listas'):
        reparar_nombres(con)
    nombre = con.execute('SELECT nombre FROM fuentes WHERE id=?', (fid,)).fetchone()['nombre']
    sin_contactos = (d.get('tipo') == 'inicio' and not any(l.get('tipo') == 'contacto' for l in d.get('listas') or []))
    res = {'ok': True, 'fuente_id': fid, 'fuente': nombre, 'posts': len(posts), 'nuevos': nuevos,
           'comentarios_nuevos': com_nuevos, 'listas_nuevas': ln, 'mensajes_nuevos': mn}
    partes = ['%s: %d publicaciones (%d nuevas)' % (nombre, len(posts), nuevos)]
    if com_nuevos:
        partes.append('%d comentarios nuevos' % com_nuevos)
    if ln:
        partes.append('%d elementos de lista nuevos' % ln)
    if mn:
        partes.append('%d mensajes nuevos' % mn)
    if bajas_txt:
        partes.append(bajas_txt)
    if foto:
        partes.append('foto')
    if sin_contactos:
        partes.append('SIN CONTACTOS: la columna de contactos no apareció en esta captura')
        res['sin_contactos'] = True
    res['resumen'] = ', '.join(partes)
    if av_res:
        res['resumen'] = av_res['resumen'] + ' | ' + res['resumen']
    return res, foto


def dir_diag_compartida():
    """Copia accesible desde Android (Descargas/FBVigia/diagnosticos), si Termux tiene acceso al almacenamiento."""
    sh = os.path.join(os.path.expanduser('~'), 'storage', 'shared')
    if os.path.isdir(sh) and os.access(sh, os.W_OK):
        d = os.path.join(sh, 'Download', 'FBVigia', 'diagnosticos')
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            return None
    return None


def guardar_diagnostico(d):
    """Guarda el informe (.txt). Los campos 'html*' largos van a archivos .html aparte. Devuelve la ruta que puedes abrir
    (la de Descargas/FBVigia/diagnosticos si existe; si no, la de fb_datos/diagnosticos)."""
    os.makedirs(ruta('diagnosticos'), exist_ok=True)
    base = '%s_%s' % (datetime.now().strftime('%Y-%m-%d-%H-%M-%S'), slug(d.get('titulo') or d.get('url') or 'pagina', 30))
    extra = []

    def saca(o, nombre):
        if isinstance(o, dict):
            return {k: (saca(v, nombre + '-' + k) if not (k.startswith('html') and isinstance(v, str) and len(v) > 1500) else guarda_html(v, nombre + '-' + k)) for k, v in o.items()}
        if isinstance(o, list):
            return [saca(v, '%s%d' % (nombre, i + 1)) for i, v in enumerate(o)]
        return o

    def guarda_html(v, nombre):
        n = '%s_%s.html' % (base, re.sub(r'[^A-Za-z0-9_-]+', '', nombre)[:50])
        extra.append((n, v))
        return '(HTML en el archivo %s)' % n

    d2 = saca(d, 'html')
    texto = 'FB Vigía - informe de diagnóstico\nGenerado: %s\n\n%s\n' % (ahora(), json.dumps(d2, ensure_ascii=False, indent=2))
    dests = [ruta('diagnosticos')]
    comp = dir_diag_compartida()
    if comp:
        dests.append(comp)
    arch = ruta('diagnosticos', base + '.txt')
    for dd in dests:
        try:
            with open(os.path.join(dd, base + '.txt'), 'w', encoding='utf-8') as f:
                f.write(texto)
            for n, v in extra:
                with open(os.path.join(dd, n), 'w', encoding='utf-8') as f:
                    f.write(v)
        except OSError:
            pass
    return os.path.join(comp, base + '.txt') if comp else arch


# ----------------------------------------------------------------------------
# descargas (imágenes y enlaces)
# ----------------------------------------------------------------------------

HOST_IMG_OK = re.compile(r'(^|\.)(fbcdn\.net|fbsbx\.com|facebook\.com|fb\.com|twimg\.com|x\.com|twitter\.com|cdninstagram\.com|instagram\.com)$', re.I)


def http_get(url, maxb=None, timeout=25):
    maxb = maxb or int(config().get('max_mb', 15)) * 1024 * 1024
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'es-ES,es;q=0.9'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(maxb + 1)
        if len(data) > maxb:
            raise ValueError('demasiado grande')
        return data, r.headers.get('Content-Type', ''), r.headers.get_content_charset()


def ext_de(ct, url=''):
    ct = (ct or '').split(';')[0].strip().lower()
    if ct == 'image/jpeg':
        return '.jpg'
    e = mimetypes.guess_extension(ct) if ct else None
    if e:
        return e
    m = re.search(r'\.(jpe?g|png|gif|webp|bmp)(?:$|\?)', url, re.I)
    return '.' + m.group(1).lower() if m else '.jpg'


def dir_imagenes():
    """Carpeta de imágenes. Si Termux tiene acceso al almacenamiento compartido, ahí (la Galería y otras
    apps de Android la ven); si no, dentro de fb_datos. Se puede fijar en config.json: carpeta_imagenes."""
    d = (config().get('carpeta_imagenes') or '').strip()
    if d:
        return os.path.expanduser(d)
    sh = os.path.join(os.path.expanduser('~'), 'storage', 'shared')
    if os.path.isdir(sh) and os.access(sh, os.W_OK):
        return os.path.join(sh, 'Download', 'FBVigia', 'imagenes')
    return ruta('imagenes')


def guardar_imagen(src, destino_base):
    """Descarga (o decodifica data:) y devuelve la ruta del archivo."""
    if src.startswith('data:'):
        m = re.match(r'data:([\w/+.-]+);base64,(.*)$', src, re.S)
        if not m:
            raise ValueError('data URI no válido')
        data, ct = base64.b64decode(m.group(2)), m.group(1)
    else:
        host = urlparse(src).hostname or ''
        if not HOST_IMG_OK.search(host):
            raise ValueError('host no permitido: ' + host)
        data, ct, _ = http_get(src)
        if 'image' not in ct.lower() and 'octet' not in ct.lower():
            raise ValueError('no es imagen (%s)' % ct)
    arch = destino_base + ext_de(ct, src)
    os.makedirs(os.path.dirname(arch), exist_ok=True)
    with open(arch, 'wb') as f:
        f.write(data)
    return arch


def camel(nombre, n=40):
    """'Diario de Ávila' -> 'DiarioDeAvila' (para nombres de archivo)."""
    t = ''.join(w[:1].upper() + w[1:] for w in re.split(r'[^A-Za-z0-9]+', sin_acentos(nombre or '')) if w)
    return t[:n] or 'Fuente'


def nombre_imagen(fecha_post, primera_vez, fnombre, fclave, pclave, k):
    """AAAA-MM-DD-HH-MM-SS_NombreDeLaFuente_clavePublicacion-imgK, con la fecha de la PUBLICACIÓN (si no se conoce, la de captura)."""
    d = (fecha_post or '').strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}', d):
        hms = re.sub(r'\D', '', d[10:])
        hms = (hms + '000000')[:6] if hms else '000000'
        f = d[:10] + '-' + hms[:2] + '-' + hms[2:4] + '-' + hms[4:6]
    else:
        n = re.sub(r'\D', '', primera_vez or '')
        n = (n + '00000000000000')[:14]
        f = '%s-%s-%s-%s-%s-%s' % (n[:4], n[4:6], n[6:8], n[8:10], n[10:12], n[12:14])
    return '%s_%s_%s-img%d' % (f, camel(fnombre or fclave), re.sub(r'[^A-Za-z0-9]', '', pclave or '')[:12] or 'post', k)


def descargar_pendientes(fuente_id=None, log=print):
    con = db()
    q = """SELECT i.id, i.url, i.pub_id, p.clave AS pclave, p.primera_vez, p.fecha_aprox, f.clave AS fclave, f.nombre AS fnombre,
                  (SELECT COUNT(*) FROM imagenes j WHERE j.pub_id=i.pub_id AND j.id<=i.id) AS k
           FROM imagenes i JOIN publicaciones p ON p.id=i.pub_id JOIN fuentes f ON f.id=p.fuente_id
           WHERE i.estado='pendiente'"""
    par = ()
    if fuente_id:
        q += ' AND f.id=?'
        par = (fuente_id,)
    filas = con.execute(q, par).fetchall()
    ok = err = 0
    for r in filas:
        base = os.path.join(dir_imagenes(), slug(r['fclave'], 60),
                            nombre_imagen(r['fecha_aprox'], r['primera_vez'], r['fnombre'], r['fclave'], r['pclave'], r['k']))
        try:
            arch = guardar_imagen(r['url'], base)
            with CANDADO:
                con.execute("UPDATE imagenes SET archivo=?, estado='ok' WHERE id=?", (arch, r['id']))
                con.commit()
            ok += 1
        except Exception as e:
            with CANDADO:
                con.execute("UPDATE imagenes SET estado=? WHERE id=?", ('error: %s' % str(e)[:80], r['id']))
                con.commit()
            err += 1
    con.close()
    if filas:
        log('Imágenes: %d descargadas, %d con error' % (ok, err))
    return ok, err


class _Texto(HTMLParser):
    SALTAR = {'script', 'style', 'noscript', 'svg', 'head', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.t, self.titulo, self._skip, self._tit = [], '', 0, False

    def handle_starttag(self, tag, attrs):
        if tag == 'title':
            self._tit = True
        if tag in self.SALTAR and tag != 'head':
            self._skip += 1
        if tag in ('p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'tr'):
            self.t.append('\n')

    def handle_endtag(self, tag):
        if tag == 'title':
            self._tit = False
        if tag in self.SALTAR and tag != 'head':
            self._skip = max(0, self._skip - 1)

    def handle_data(self, d):
        if self._tit:
            self.titulo += d
        elif not self._skip:
            self.t.append(d)


def seguir_enlace_externo(url, destino_base):
    data, ct, cs = http_get(url, maxb=3 * 1024 * 1024)
    if 'html' not in ct.lower() and 'text' not in ct.lower():
        arch = destino_base + ext_de(ct, url)
        open(arch, 'wb').write(data)
        return arch, '(archivo %s)' % ct.split(';')[0]
    txt = data.decode(cs or 'utf-8', errors='replace')
    p = _Texto()
    p.feed(txt)
    cuerpo = re.sub(r'\n\s*\n+', '\n\n', re.sub(r'[ \t]+', ' ', ''.join(p.t))).strip()
    titulo = re.sub(r'\s+', ' ', p.titulo).strip()
    arch = destino_base + '.txt'
    with open(arch, 'w', encoding='utf-8') as f:
        f.write('URL: %s\nTítulo: %s\nDescargado: %s\n\n%s\n' % (url, titulo, ahora(), cuerpo))
    return arch, titulo


# ----------------------------------------------------------------------------
# servidor local (recibe lo que envía la extensión)
# ----------------------------------------------------------------------------


class Manejador(BaseHTTPRequestHandler):
    server_version = 'FBVigia/' + VERSION

    def log_message(self, fmt, *a):
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Private-Network', 'true')

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b)))
        self._cors()
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _origen_ok(self):
        # Una página web cualquiera no debe poder escribir aquí: solo la extensión
        # (Origin moz-extension://...) o herramientas sin Origin (curl).
        o = self.headers.get('Origin', '')
        return not o or o.startswith('moz-extension://') or o.startswith('chrome-extension://')

    def do_GET(self):
        ruta_ = urlparse(self.path).path
        if ruta_ == '/ping':   # responde AUNQUE la base esté ocupada (si no, un servidor con la base bloqueada parecería «apagado»)
            try:
                c_ = sqlite3.connect(DB, timeout=2)
                nf_ = c_.execute('SELECT COUNT(*) FROM fuentes').fetchone()[0]
                npd_ = c_.execute("SELECT COUNT(*) FROM cola WHERE estado='pendiente'").fetchone()[0]
                c_.close()
            except Exception:
                nf_ = npd_ = None
            return self._json({'ok': True, 'programa': 'fbvigia', 'version': VERSION, 'datos': os.path.basename(DIR.rstrip('/')), 'fuentes': nf_ if nf_ is not None else 0, 'pendientes': npd_ if npd_ is not None else 0})
        con = db()
        try:
            if ruta_ == '/leidas':   # claves de las publicaciones de esa fuente ya leídas a fondo HOY
                q_ = parse_qs(urlparse(self.path).query)
                ff = fuente_por_clave(con, (q_.get('clave') or [''])[0])
                cl = []
                cont_ = {}
                if ff:
                    ids_ = misma_fuente(con, ff) or {ff['id']}
                    for r in con.execute('SELECT clave, ncomentarios, reacciones, ncompartidos FROM publicaciones WHERE leida_dia=? AND COALESCE(texto_cortado,0)=0 AND fuente_id IN (%s)' % ','.join('?' * len(ids_)),
                                         [datetime.now().strftime('%Y-%m-%d')] + list(ids_)).fetchall():
                        cl.append(r[0])
                        cont_[r[0]] = {'ncomentarios': r[1] or 0, 'reacciones': r[2] or 0, 'ncompartidos': r[3] or 0}
                return self._json({'ok': True, 'claves': cl, 'conteos': cont_})
            if ruta_ == '/cola':
                with CANDADO:
                    for r in con.execute("SELECT id, actualizada FROM cola WHERE estado IN ('abriendo','en_curso')").fetchall():
                        if delta_seg(r['actualizada']) > 900:
                            con.execute("UPDATE cola SET estado='error', detalle='sin respuesta de la extensión', actualizada=? WHERE id=?", (ahora(), r['id']))
                    con.commit()
                items = []
                with CANDADO:
                    limpiar_cola_obsoleta(con)
                    con.commit()
                for r in con.execute("SELECT * FROM cola WHERE estado='pendiente' ORDER BY id").fetchall():
                    if r['origen'] == 'abrir' and delta_seg(r['creada']) < 180:
                        continue
                    items.append({'id': r['id'], 'url': r['url'], 'scrolls': r['scrolls'], 'accion': r['accion'] or 'capturar', 'pub_id': r['pub_id']})
                return self._json({'ok': True, 'items': items})
            return self._json({'ok': False, 'error': 'ruta desconocida'}, 404)
        finally:
            con.close()

    def do_POST(self):
        if not self._origen_ok():
            return self._json({'ok': False, 'error': 'origen no permitido'}, 403)
        n = int(self.headers.get('Content-Length') or 0)
        if n > 80 * 1024 * 1024:
            return self._json({'ok': False, 'error': 'demasiado grande'}, 413)
        try:
            d = json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception as e:
            return self._json({'ok': False, 'error': 'JSON no válido: %s' % e}, 400)
        ruta_ = urlparse(self.path).path
        con = db()
        try:
            if ruta_ == '/captura':
                with CANDADO:
                    res, foto = procesar_captura(con, d)
                    if d.get('diagnostico'):
                        res['informe'] = guardar_diagnostico(d['diagnostico'])
                        res['resumen'] += ' | AVISO: 0 publicaciones leídas; informe en ' + res['informe']
                    if d.get('nota_rango'):
                        res['resumen'] += ' | ' + d['nota_rango']
                    elif d.get('cola_id'):
                        cr_ = con.execute('SELECT url FROM cola WHERE id=?', (d['cola_id'],)).fetchone()
                        if cr_ and 'fbv-rango' in (cr_['url'] or ''):
                            res['resumen'] += ' | AVISO: la extensión instalada no busca fechas (es anterior a la 1.7.1): reinstala fbvigia.xpi'
                    if d.get('diag_vacios'):
                        arch_v = guardar_diagnostico({'titulo': 'posts-sin-texto', 'url': d.get('url'), 'publicaciones_sin_texto': d['diag_vacios']})
                        res['resumen'] += ' | %d publicaciones sin texto; informe: %s' % (len(d['diag_vacios']), arch_v)
                    if d.get('diag_fechas'):
                        res['informe_fechas'] = guardar_diagnostico({'titulo': 'fechas-sin-leer', 'url': d.get('url'), 'publicaciones_sin_fecha': d['diag_fechas']})
                        res['resumen'] += ' | %d sin fecha leíble; informe: %s' % (len(d['diag_fechas']), res['informe_fechas'])
                    if d.get('cola_id'):
                        con.execute("UPDATE cola SET estado='hecha', actualizada=? WHERE id=?", (ahora(), d['cola_id']))
                    con.commit()
                print('[%s] %s' % (datetime.now().strftime('%H:%M:%S'), res['resumen']), flush=True)
                cfg = config()
                if foto:
                    threading.Thread(target=self._foto, args=(foto,), daemon=True).start()
                if cfg.get('auto_imagenes', True):
                    threading.Thread(target=lambda: descargar_pendientes(res['fuente_id'], log=lambda m: print('   ' + m, flush=True)), daemon=True).start()
                return self._json(res)
            if ruta_ == '/diagnostico':
                arch = guardar_diagnostico(d)
                print('[%s] Informe de diagnóstico: %s' % (datetime.now().strftime('%H:%M:%S'), arch), flush=True)
                return self._json({'ok': True, 'archivo': arch})
            if ruta_ == '/detalle':
                t0 = ahora()
                nr = ns = 0
                with CANDADO:
                    for r in d.get('reacciones') or []:
                        k = norm_url(r.get('url') or '') or (r.get('nombre') or '')
                        con.execute('INSERT INTO reaccionantes(pub_id,clave,nombre,url,reaccion,primera_vez) VALUES(?,?,?,?,?,?) '
                                    'ON CONFLICT(pub_id,clave) DO UPDATE SET reaccion=excluded.reaccion',
                                    (d['pub_id'], k, nombre_sin_seguir(r.get('nombre')), r.get('url'), r.get('reaccion') or '?', t0))
                        nr += 1
                    for r in d.get('compartidos') or []:
                        k = norm_url(r.get('url') or '') or (r.get('nombre') or '')
                        con.execute('INSERT OR IGNORE INTO compartidores(pub_id,clave,nombre,url,primera_vez) VALUES(?,?,?,?,?)',
                                    (d['pub_id'], k, nombre_sin_seguir(r.get('nombre')), r.get('url'), t0))
                        ns += 1
                    ncr = 0
                    for cr in d.get('comentarios_reacciones') or []:
                        cm_ = con.execute('SELECT id FROM comentarios WHERE pub_id=? AND clave=?', (d['pub_id'], cr.get('clave'))).fetchone()
                        if not cm_:
                            continue
                        if cr.get('n'):
                            con.execute('UPDATE comentarios SET reacciones=? WHERE id=?', (int(cr['n']), cm_['id']))
                        for r in cr.get('reacciones') or []:
                            k = norm_url(r.get('url') or '') or (r.get('nombre') or '')
                            con.execute('INSERT INTO reac_comentarios(com_id,clave,nombre,url,reaccion,primera_vez) VALUES(?,?,?,?,?,?) '
                                        'ON CONFLICT(com_id,clave) DO UPDATE SET reaccion=excluded.reaccion',
                                        (cm_['id'], k, nombre_sin_seguir(r.get('nombre')), r.get('url'), r.get('reaccion') or '?', t0))
                            ncr += 1
                    ncom_n = guardar_comentarios_detalle(con, d['pub_id'], d.get('comentarios') or [], t0)
                    if d.get('n_comentarios_fb') is not None:
                        con.execute('UPDATE publicaciones SET ncomentarios=? WHERE id=? AND COALESCE(ncomentarios,0)<?', (int(d['n_comentarios_fb']), d['pub_id'], int(d['n_comentarios_fb'])))
                    con.execute('UPDATE publicaciones SET detalle_vez=? WHERE id=?', (t0, d['pub_id']))
                    registrar_pasada_detalle(con, d['pub_id'], d, t0)
                    if d.get('lista_limitada'):   # Facebook solo enseña los nombres de los AMIGOS que reaccionaron (páginas con muchas reacciones)
                        des = ', '.join('%s %s' % (x.get('emoji') or '?', x.get('n')) for x in (d.get('desglose') or []) if x.get('n') is not None)
                        con.execute('UPDATE publicaciones SET detalle_limitado=? WHERE id=?', (des or 'limitada', d['pub_id']))
                    else:
                        con.execute('UPDATE publicaciones SET detalle_limitado=NULL WHERE id=?', (d['pub_id'],))
                    for cr in d.get('comentarios_reacciones') or []:
                        if cr.get('limitada'):
                            con.execute('UPDATE comentarios SET lista_limitada=1 WHERE pub_id=? AND clave=?', (d['pub_id'], cr.get('clave')))
                    nota = '%d reacciones, %d compartidos' % (nr, ns) + (', %d reacciones en comentarios' % ncr if ncr else '') + (', %d comentarios nuevos (leídos %d)' % (ncom_n, len(d.get('comentarios') or [])) if d.get('comentarios') is not None else '')
                    if d.get('notas'):
                        nota += ' | ' + '; '.join(d['notas'])
                    if d.get('cola_id'):
                        con.execute("UPDATE cola SET estado='hecha', detalle=?, actualizada=? WHERE id=?", (nota, ahora(), d['cola_id']))
                    con.commit()
                if d.get('diag'):
                    d['diag']['titulo'] = 'detalle publicacion %s' % d['pub_id']
                    arch = guardar_diagnostico(d['diag'])
                    print('   Detalle sin personas: informe en %s' % arch, flush=True)
                if d.get('diag_com'):
                    d['diag_com']['titulo'] = 'comentarios incompletos publicacion %s' % d['pub_id']
                    arch = guardar_diagnostico(d['diag_com'])
                    print('   Comentarios incompletos (leídos %s de %s): informe en %s' % (d['diag_com'].get('leidos'), d['diag_com'].get('facebook_indica'), arch), flush=True)
                print('[%s] Detalle de la publicación #%s: %d reacciones, %d compartidos, %d comentarios leídos' % (datetime.now().strftime('%H:%M:%S'), d['pub_id'], nr, ns, len(d.get('comentarios') or [])), flush=True)
                return self._json({'ok': True, 'reacciones': nr, 'compartidos': ns,
                                   'resumen': '%d reacciones, %d compartidos' % (nr, ns)})
            if ruta_ == '/busqueda':
                t0 = ahora()
                n = 0
                with CANDADO:
                    ya = con.execute('SELECT COALESCE(MAX(n),0) FROM resultados WHERE busq_id=?', (d['busq_id'],)).fetchone()[0]
                    for r in d.get('resultados') or []:
                        u = r.get('url') or ''
                        k = norm_url(u) if u else (r.get('titulo') or '')
                        if con.execute('SELECT 1 FROM resultados WHERE busq_id=? AND norm=?', (d['busq_id'], k)).fetchone():
                            continue
                        ya += 1
                        n += 1
                        con.execute('INSERT INTO resultados(busq_id,n,tipo,titulo,detalle,url,norm) VALUES(?,?,?,?,?,?,?)',
                                    (d['busq_id'], ya, r.get('tipo'), r.get('titulo'), r.get('detalle'), u, k))
                    con.execute('UPDATE busquedas SET n_resultados=? WHERE id=?', (ya, d['busq_id']))
                    if d.get('diagnostico'):
                        guardar_diagnostico(d['diagnostico'])
                    if d.get('cola_id'):
                        con.execute("UPDATE cola SET estado='hecha', detalle=?, actualizada=? WHERE id=?",
                                    ('%d resultados' % n, ahora(), d['cola_id']))
                    con.commit()
                print('[%s] Búsqueda #%s: %d resultados' % (datetime.now().strftime('%H:%M:%S'), d['busq_id'], n), flush=True)
                return self._json({'ok': True, 'n': n, 'resumen': '%d resultados' % n})
            if ruta_ == '/cola/reclamar':
                n_ = norm_url(d.get('url', ''))
                with CANDADO:
                    r = con.execute("SELECT * FROM cola WHERE estado='pendiente' AND norm=? ORDER BY id LIMIT 1", (n_,)).fetchone()
                    if r:
                        con.execute("UPDATE cola SET estado='en_curso', actualizada=? WHERE id=?", (ahora(), r['id']))
                        con.commit()
                        return self._json({'ok': True, 'job': {'id': r['id'], 'url': r['url'], 'scrolls': r['scrolls'], 'accion': r['accion'] or 'capturar',
                                                               'pub_id': r['pub_id'], 'cerrar': r['origen'] == 'abrir'}})
                return self._json({'ok': True, 'job': None})
            if ruta_ == '/cola/latido':   # la extensión avisa de que sigue leyendo (evita que se dé por muerta)
                with CANDADO:
                    det_ = str(d.get('detalle', ''))[:200]
                    if det_:
                        con.execute("UPDATE cola SET estado='en_curso', detalle=?, actualizada=? WHERE id=? AND estado IN ('abriendo','en_curso')",
                                    (det_, ahora(), d.get('id')))
                    else:   # latido del segundo plano: solo "sigo vivo", sin pisar el último texto
                        con.execute("UPDATE cola SET estado='en_curso', actualizada=? WHERE id=? AND estado IN ('abriendo','en_curso')", (ahora(), d.get('id')))
                    if 'oculta' in d:   # la pestaña está oculta (Firefox en segundo plano): Termux lo trae al frente
                        con.execute('UPDATE cola SET oculta=? WHERE id=?', (1 if d.get('oculta') else 0, d.get('id')))
                    cu = d.get('cursor')
                    if cu and cu.get('clave'):   # marcador: hasta qué publicación se ha llegado en la búsqueda por fechas
                        cr_ = con.execute('SELECT url FROM cola WHERE id=?', (d.get('id'),)).fetchone()
                        if cr_ and cr_['url']:
                            con.execute('INSERT OR REPLACE INTO cursores(fuente, clave, dn, dia, actualizada) VALUES(?,?,?,?,?)',
                                        (clave_de_url(cr_['url']), cu['clave'], int(cu.get('dn') or 0), datetime.now().strftime('%Y-%m-%d'), ahora()))
                    con.commit()
                return self._json({'ok': True})
            if ruta_ == '/cola/estado':
                with CANDADO:
                    con.execute('UPDATE cola SET estado=?, detalle=?, actualizada=? WHERE id=?',
                                (d.get('estado'), d.get('detalle', ''), ahora(), d.get('id')))
                    con.commit()
                return self._json({'ok': True})
            return self._json({'ok': False, 'error': 'ruta desconocida'}, 404)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._json({'ok': False, 'error': str(e)}, 500)
        finally:
            con.close()

    @staticmethod
    def _foto(foto):
        try:
            base = os.path.join(dir_imagenes(), 'fotos', 'foto_%s' % hashlib.sha1(foto['url'].encode()).hexdigest()[:10])
            arch = guardar_imagen(foto['src'], base)
            if foto.get('texto') or foto.get('alt'):
                open(base + '.txt', 'w', encoding='utf-8').write('URL: %s\n\n%s\n%s\n' % (foto['url'], foto.get('texto', ''), foto.get('alt', '')))
            con = db()
            with CANDADO:
                con.execute("UPDATE enlaces SET estado='hecho', archivo=?, detalle='foto' WHERE norm=?", (arch, norm_url(foto['url'])))
                con.commit()
            con.close()
            print('   Foto guardada: %s' % arch, flush=True)
        except Exception as e:
            print('   Foto no guardada: %s' % e, flush=True)


PUERTOS_CONOCIDOS = {8765: 'foroleer / bop / pesthist / samar (por defecto de cada uno: chocan entre sí)', 8766: 'empinf', 8777: 'swn',
                     8781: 'fb.py', 8782: 'x.py', 8783: 'ig.py'}


def cmd_puertos(a):
    """Qué puertos de 8760 a 8800 están ocupados ahora mismo y de quién es cada uno (según el reparto recomendado)."""
    import socket
    print('Puertos locales ocupados ahora (8760-8800):')
    n = 0
    for p_ in range(8760, 8801):
        s_ = socket.socket()
        s_.settimeout(0.3)
        try:
            s_.connect(('127.0.0.1', p_))
            ocupado = True
        except OSError:
            ocupado = False
        finally:
            s_.close()
        if ocupado:
            n += 1
            print('  %d  OCUPADO   %s' % (p_, PUERTOS_CONOCIDOS.get(p_, '(no está en el reparto recomendado)')))
    if not n:
        print('  ninguno (no hay ningún servidor de tus herramientas en marcha)')
    print('\nReparto recomendado (cada herramienta con el suyo):')
    for p_, q in sorted(PUERTOS_CONOCIDOS.items()):
        print('  %d  %s' % (p_, q))
    print('Cambiar el de fb.py: python3 fb.py puerto N      (ver NOVEDAD 1.44 del manual para las demás)')


def cmd_puerto(a):
    """Muestra o cambia el puerto del servidor de fb.py (para que no choque con otros programas, p. ej. la extensión SAMAR)."""
    actual = int(config()['puerto'])
    if a.numero is None:
        q, det = quien_usa_puerto(actual)
        print('Puerto de fb.py: %d   (%s)' % (actual, {'fbvigia': 'ocupado por fb.py: bien', 'otro': 'ocupado por OTRO programa: choca con fb.py',
                                                         'mudo': 'ocupado por algo que no responde', 'libre': 'libre'}[q]))
        if q in ('otro', 'mudo'):
            print('Cámbialo:   python3 fb.py puerto %d' % (puerto_libre_cerca(8790) or 8790))
        print('Puertos de x.py e ig.py: 8782 y 8783. Evita esos (y los de tus otras herramientas: python3 fb.py puertos).')
        return
    n = int(a.numero)
    if not 1024 <= n <= 65535:
        sys.exit('Usa un puerto entre 1024 y 65535 (por ejemplo 8790).')
    q, det = quien_usa_puerto(n)
    if q in ('otro', 'mudo') and not a.forzar:
        sys.exit('El puerto %d ya lo usa otro programa. Elige otro (sugerencia: %s) o usa --forzar.' % (n, puerto_libre_cerca(8790) or 8790))
    guardar_config(puerto=n)
    print('Listo: fb.py usará el puerto %d (guardado en config.json).' % n)
    print('AHORA, en la extensión FB Vigía: abre su icono, en «Servidor de Termux» pon  http://127.0.0.1:%d  y pulsa «Guardar ajustes».' % n)
    print('Si ya había un fb.py en marcha, páralo (Ctrl+C) y vuelve a lanzar.')


def cmd_servidor(a):
    c = config()
    puerto = a.puerto or int(c['puerto'])
    db().close()
    srv = crear_servidor(puerto)
    print('FB Vigía %s - servidor en http://127.0.0.1:%d  (Ctrl+C para parar)' % (VERSION, puerto))
    print('Datos en: %s' % DIR)
    print('Esperando capturas de la extensión...', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nServidor detenido.')


# ----------------------------------------------------------------------------
# comandos de consulta
# ----------------------------------------------------------------------------


REF_RE = re.compile(r'^([CPGASMV])(\d+)$', re.I)
LETRA_TIPO = {'C': 'contacto', 'P': 'pagina', 'G': 'grupo', 'A': 'amigo', 'S': 'siguiendo', 'M': 'miembro', 'V': 'conversacion'}
TIPO_LETRA = {v: k for k, v in LETRA_TIPO.items()}


def url_de_lista(l):
    """URL de perfil/página/grupo de un elemento de lista (los contactos vienen como enlace de Messenger)."""
    u = url_grupo_base(l['url'] or '')
    m = re.search(r'/messages/t/(\d+)/?$', u)
    if m:
        return 'https://www.facebook.com/profile.php?id=' + m.group(1)
    if '/messages/t/' in u:
        return ''
    m = re.search(r'/groups/[^/]+/user/(\d+)', u)
    if m:
        return 'https://www.facebook.com/profile.php?id=' + m.group(1)
    return u


def fuente_de_lista(con, l):
    """La fuente de una referencia (P141...). Si la misma página está guardada en varias fuentes (clave por nombre, por id, por
    alias...), se elige la que tiene publicaciones: así 'ultimas P141' no se queda en una fuente vacía duplicada."""
    cands = []

    def anade(f):
        if f and f['id'] not in [x['id'] for x in cands]:
            cands.append(f)
    ks = [l['clave']]
    if (l['clave'] or '').startswith('msg-'):
        ks.append('id-' + l['clave'][4:])
    for c in ks:
        anade(con.execute('SELECT * FROM fuentes WHERE clave=?', (c,)).fetchone())
    u = url_de_lista(l)
    if u:
        ck = clave_de_url(u)          # la misma página puede estar guardada con otra clave (nombre <-> id) o por alias
        for c in ([l['clave']] + ([ck] if ck else [])):
            anade(fuente_por_clave(con, c))
        n = norm_url(u)
        for f in con.execute("SELECT * FROM fuentes WHERE url<>''").fetchall():
            if norm_url(f['url']) == n:
                anade(f)

    def npub(f):
        return con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (f['id'],)).fetchone()[0]
    mejor = max(cands, key=lambda f: (npub(f), -f['id'])) if cands else None
    if mejor is not None and npub(mejor) > 0:
        return mejor
    nom = (l['nombre'] or '').strip().lower()   # mismo nombre: la única página/grupo con ese nombre que sí tiene publicaciones
    if nom:
        fs = [x for x in con.execute("SELECT * FROM fuentes WHERE lower(nombre)=? AND tipo IN ('pagina','grupo','persona_grupo')", (nom,)).fetchall()]
        con_pub = [x for x in fs if npub(x) > 0]
        if len(con_pub) == 1:
            return con_pub[0]
        if mejor is None and len(fs) == 1:
            return fs[0]
    return mejor


def lista_por_ref(con, ref):
    m = REF_RE.match(str(ref).strip())
    if not m:
        return None
    l = con.execute('SELECT * FROM listas WHERE id=?', (int(m.group(2)),)).fetchone()
    if not l or TIPO_LETRA.get(l['tipo']) != m.group(1).upper():
        sys.exit('No existe la referencia %s. Mira los números con: python fb.py listas paginas | grupos | contactos ...' % ref.upper())
    return l


def fuente_de_ref_crear(con, l):
    """La fuente de un elemento de lista (P289, G3...). Si aún no existe (nunca se ha capturado), se crea SIN vigilar, solo para
    poder guardar en ella datos permanentes como la cantidad de publicaciones a leer."""
    f = fuente_de_lista(con, l)
    if f:
        return f
    u = url_de_lista(l)
    ck = clave_de_url(u) if u else None
    if not ck:
        return None
    nombre = limpia_nombre(l['nombre']) or l['nombre'] or ck
    cur = con.execute('INSERT INTO fuentes(clave,tipo,nombre,url,creada,vigilada,bloqueada) VALUES(?,?,?,?,?,0,0)',
                      (ck, 'grupo' if ck.startswith('grupo-') else 'pagina', nombre, u, ahora()))
    return con.execute('SELECT * FROM fuentes WHERE id=?', (cur.lastrowid,)).fetchone()


def buscar_fuente(con, q, silencioso=False, crear=False):
    if q is None:
        return None
    l = lista_por_ref(con, q)
    if l:
        f = fuente_de_lista(con, l)
        if not f and crear:
            f = fuente_de_ref_crear(con, l)
        if f:
            return f
        sys.exit('"%s" todavía no tiene publicaciones capturadas.\nCaptúralas con:  python fb.py vigilar %s' % (l['nombre'], str(q).upper()))
    if str(q).isdigit():
        f = con.execute('SELECT * FROM fuentes WHERE id=?', (int(q),)).fetchone()
        if f:
            return f
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', (q,)).fetchone()
    if f:
        return f
    like = '%' + q.lower() + '%'
    fs = con.execute('SELECT * FROM fuentes WHERE lower(nombre) LIKE ? OR lower(clave) LIKE ? OR lower(url) LIKE ?', (like, like, like)).fetchall()
    if len(fs) == 1:
        return fs[0]
    if not fs:
        if not silencioso:
            sys.exit('No hay ninguna fuente que coincida con "%s". Mira "python fb.py fuentes".' % q)
        return None
    print('"%s" coincide con varias fuentes; indica el número:' % q)
    print(tabla_c(['Nº', 'Tipo', 'Nombre'], [[f['id'], f['tipo'], f['nombre']] for f in fs]))
    sys.exit(1)


def cmd_fuentes(a):
    con = db()
    filas = []
    for f in con.execute('SELECT * FROM fuentes ORDER BY id').fetchall():
        npub = con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (f['id'],)).fetchone()[0]
        nnue = con.execute("""SELECT COUNT(*) FROM publicaciones p WHERE fuente_id=? AND (visto=0 OR
                 EXISTS(SELECT 1 FROM comentarios c WHERE c.pub_id=p.id AND c.visto=0))""", (f['id'],)).fetchone()[0]
        filas.append([f['id'], f['tipo'], f['nombre'], npub, nnue, f['n_capturas'],
                      (f['ultima_captura'] or '-')[:16], 'sí' if f['vigilada'] else 'no'])
    cats = categorias_fuentes(con)
    for fila in filas:
        fila[1] = ','.join(sorted(cats.get(fila[0], {fila[1]})))
    if not filas:
        print('Todavía no hay fuentes. Arranca el servidor y captura una página con la extensión.')
        return
    print(tabla_c(['Nº', 'Categoría', 'Nombre', 'Public.', 'Nuevas', 'Captur.', 'Última captura', 'Vigilada'], filas))


def _post_nuevo_sql():
    return "(p.visto=0 OR EXISTS(SELECT 1 FROM comentarios c WHERE c.pub_id=p.id AND c.visto=0))"


OPC = {'enlaces': set(), 'reacciones': False}
_CATS = None
EMOJI_TXT = {'Me gusta': '👍', 'Me encanta': '❤️', 'Me importa': '🥰', 'Me divierte': '😆', 'Me asombra': '😮',
             'Me entristece': '😢', 'Me enfada': '😡'}


def parse_enlaces(txt):
    if not txt:
        return set()
    t = txt.strip().lower()
    if t in ('todos', 'todo', 'all', 'a'):
        return {1, 2, 3}
    r = set()
    for x in re.split(r'[,\s]+', t):
        if x in ('1', '2', '3'):
            r.add(int(x))
        elif x:
            sys.exit('--enlaces: usa 1, 2, 3 (separados por comas) o "todos".  1=quien publica  2=quienes comentan  3=quienes reaccionan o comparten')
    return r


def aplicar_opciones(a):
    OPC['enlaces'] = parse_enlaces(getattr(a, 'enlaces', None))
    OPC['reacciones'] = bool(getattr(a, 'reacciones', False))


def url_perfil(u):
    if not u:
        return ''
    m = re.search(r'/groups/[^/]+/user/(\d+)', u)
    if m:
        return 'https://www.facebook.com/profile.php?id=' + m.group(1)
    return re.sub(r'[?&]comment_id=[^&]*', '', u)


def corto_tiempo(t):
    """'hace 42 minutos' -> 'hace 42m';  'Ayer a las 18:30' -> 'ayer 18:30'."""
    t = (t or '').strip().lower()
    if not t:
        return ''
    if t in ('justo ahora', 'ahora', 'just now'):
        return 'ahora'
    m = re.match(r'^(?:hace\s+)?(\d+)\s*(segundos?|seg|s|minutos?|min|m|horas?|hrs?|h|d[ií]as?|d|semanas?|sem|w|meses|mes|a[ñn]os?|a|y)\b', t)
    if m:
        n, u = m.group(1), m.group(2)
        if u.startswith('sem') or u == 'w':
            suf = 'sem'
        elif u.startswith('s'):
            suf = 's'
        elif u.startswith('min') or u == 'm':
            suf = 'm'
        elif u.startswith('h'):
            suf = 'h'
        elif u.startswith('d'):
            suf = 'd'
        elif u.startswith('mes'):
            suf = 'mes'
        else:
            suf = 'a'
        return 'hace %s%s' % (n, suf)
    return re.sub(r'\s+a las\s+', ' ', t)


def fecha_corta(f):
    return (f or '').replace(' ', '-').replace(':', '-')


def etiqueta_fuente(con, fid, destino=''):
    global _CATS
    f = con.execute('SELECT tipo, nombre FROM fuentes WHERE id=?', (fid,)).fetchone()
    if not f:
        return ''
    if f['tipo'].startswith('x_'):
        if f['tipo'] == 'x_cuenta':
            return 'X:%s' % f['nombre']
        if f['tipo'] == 'x_busqueda':
            return 'Búsqueda X:%s' % re.sub(r'^Búsqueda:\s*', '', f['nombre'])
        if f['tipo'] == 'x_hilo':
            return 'X:hilo'
        return 'X:%s' % re.sub(r'^X:\s*', '', f['nombre'])
    if f['tipo'].startswith('ig_'):
        if f['tipo'] == 'ig_cuenta':
            return 'IG:%s' % f['nombre']
        if f['tipo'] == 'ig_hilo':
            return 'IG:publicación'
        return 'IG:%s' % re.sub(r'^Instagram:\s*', '', f['nombre'])
    if f['tipo'] == 'persona_grupo':
        return 'Grupo:%s' % destino if destino else ''
    if f['tipo'] == 'grupo':
        return 'Grupo:%s' % f['nombre']
    if f['tipo'] == 'inicio':
        return 'Inicio'
    if f['tipo'] == 'pagina':
        if _CATS is None:
            _CATS = categorias_fuentes(con)
        cats = _CATS.get(fid, set())
        return '%s:%s' % ('Persona' if (cats & {'contactos', 'amigos', 'personas'}) and 'paginas' not in cats else 'Página', f['nombre'])
    return '%s:%s' % (f['tipo'].capitalize(), f['nombre'])


def mostrar_post(con, p, marcar=True, com=True, fnombre=None):
    nuevos_com = con.execute('SELECT COUNT(*) FROM comentarios WHERE pub_id=? AND visto=0', (p['id'],)).fetchone()[0]
    etiqueta = '[NUEVA] ' if p['visto'] == 0 else ('[COMENTARIOS NUEVOS] ' if nuevos_com else '')
    es_ig = bool(re.search(r'^https?://(www\.)?instagram\.com/', p['permalink'] or p['autor_url'] or ''))
    es_x = bool(re.search(r'^https?://(www\.)?(x|twitter)\.com/', p['permalink'] or p['autor_url'] or '')) or es_ig
    quien_ = sin_estilo(p['autor']) or '(sin autor)'
    if es_ig:
        quien_ = '@' + quien_.lstrip('@') if p['autor_url'] else quien_
    elif es_x:
        hm = re.search(r'(?:x|twitter)\.com/([^/?#]+)', p['autor_url'] or p['permalink'] or '')
        if hm:
            quien_ += ' @' + hm.group(1)
    cab = '%s#%d %s' % (etiqueta, p['id'], quien_)
    if p['tiempo_texto']:
        cab += '-' + corto_tiempo(p['tiempo_texto'])
        if p['fecha_aprox']:
            cab += ' (aprox. %s)' % fecha_corta(p['fecha_aprox'])
    else:
        cab += '-captada ' + fecha_corta(p['primera_vez'][:16])
    ef = etiqueta_fuente(con, p['fuente_id'], p['destino'] or '')
    if ef:
        cab += '-' + ef
    print(cab)
    if 1 in OPC['enlaces'] and p['autor_url']:
        print('    perfil: ' + url_perfil(p['autor_url']))
    if p['compartido_de']:
        _k = p.keys()
        _cf = p['compartido_fecha'] if 'compartido_fecha' in _k and p['compartido_fecha'] else ''
        print(('    %s' if es_x else '    compartida de: %s') % (p['compartido_de'] + ((' (original del %s)' % _cf) if _cf else '')))
        if 'compartido_url' in _k and p['compartido_url']:
            print('    original: %s%s' % (p['compartido_url'], ('  (leída como #%d)' % p['original_id']) if 'original_id' in _k and p['original_id'] else '  (para leerla: python3 fb.py original %d)' % p['id']))
    imgs = con.execute('SELECT archivo, estado, ocr, descripcion, alt FROM imagenes WHERE pub_id=? ORDER BY id', (p['id'],)).fetchall()
    cuerpo = p['texto'] or ''
    if p['texto_compartido']:
        cuerpo += '\n[compartido] ' + p['texto_compartido']
    if cuerpo.strip():
        print(envolver(cuerpo))
        if 'compartido_cortado' in p.keys() and p['compartido_cortado'] and p['texto_compartido']:
            print('    [el pie del reel/vídeo compartido sale CORTADO: Facebook solo da el principio sin abrir el reel; para ver más, ábrelo tú en la app]')
        if 'texto_cortado' in p.keys() and p['texto_cortado']:
            print('    [texto posiblemente CORTADO: no se llegó a pulsar «Ver más»; se relee con:  python3 fb.py detalle %d --releer ]' % p['id'])
    elif imgs:
        print('    [solo %s]' % ('1 imagen' if len(imgs) == 1 else '%d imágenes' % len(imgs)))
    else:
        med_ = con.execute("SELECT url FROM enlaces WHERE pub_id=? AND (url LIKE '%/reel/%' OR url LIKE '%/videos/%' OR url LIKE '%/watch%') LIMIT 1", (p['id'],)).fetchone()
        vis_ = [l for l in (p['texto_visible'] or '').split('\n')[2:6] if '\u034f' not in l]   # las letras sueltas con U+034F son texto señuelo de Facebook
        if med_:
            print('    [solo %s sin texto] ' % ('REEL' if RE_REEL.search(med_[0] or '') else 'vídeo/reel') + ' / '.join(vis_)[:150])
        else:
            print(envolver('(sin texto)  ' + ' / '.join(vis_)))
    for i, im in enumerate(imgs, 1):
        if im['estado'] == 'ok' and im['archivo']:
            print('    Img%d: %s' % (i, os.path.realpath(im['archivo'])))
        elif im['estado'] == 'pendiente':
            print('    Img%d: (sin descargar todavía; ejecuta: python fb.py imagenes)' % i)
        else:
            print('    Img%d: (no se pudo descargar; vuelve a capturar la página)' % i)
    leidas = [(i, im) for i, im in enumerate(imgs, 1) if (im['ocr'] or '').strip() or (im['descripcion'] or '').strip()]
    if leidas:
        print('    --- Lo que hay en las imágenes ---')
        for i, im in leidas:
            if (im['descripcion'] or '').strip():
                print(envolver('Imagen %d, se ve: %s' % (i, ' '.join(im['descripcion'].split()))))
            if (im['ocr'] or '').strip():
                print(envolver('Imagen %d, texto: %s' % (i, ' '.join(im['ocr'].split()))))
    ne = con.execute('SELECT COUNT(*) FROM enlaces WHERE pub_id=?', (p['id'],)).fetchone()[0]
    datos = []
    if es_x:
        if p['reacciones'] is not None:
            datos.append('me gusta:%s' % p['reacciones'])
        if p['ncomentarios'] is not None:
            datos.append(('comentarios:%s' if es_ig else 'respuestas:%s') % p['ncomentarios'])
        if p['ncompartidos'] is not None:
            datos.append('reposts:%s' % p['ncompartidos'])
        if p['visibilidad']:
            datos.append(p['visibilidad'].replace(' · ', ' '))
    else:
        if p['reacciones'] is not None:
            datos.append('reacciones:%s' % p['reacciones'])
        if p['ncomentarios'] is not None:
            nl_ = con.execute('SELECT COUNT(*) FROM comentarios WHERE pub_id=?', (p['id'],)).fetchone()[0]
            datos.append('comentarios:%s' % p['ncomentarios'] + (' (leídos %d)' % nl_ if nl_ < int(p['ncomentarios'] or 0) else ''))
        if p['ncompartidos'] is not None:
            datos.append('compartida:%s' % p['ncompartidos'])
    if ne:
        datos.append('enlaces:%d' % ne)
    try:
        if not es_x and url_reel(con, p):
            datos.append('[REEL: detalle no disponible]')
    except Exception:
        pass
    if datos:
        print('    ' + ' '.join(datos))
    if p['permalink']:
        print('    ' + p['permalink'])
    if OPC['reacciones'] or 3 in OPC['enlaces']:
        rs = con.execute('SELECT * FROM reaccionantes WHERE pub_id=? ORDER BY reaccion, nombre', (p['id'],)).fetchall()
        ss = con.execute('SELECT * FROM compartidores WHERE pub_id=? ORDER BY nombre', (p['id'],)).fetchall()
        if not rs and not ss and not p['detalle_vez']:
            print('    (NO se ha consultado todavía quién reaccionó/compartió: "post" solo muestra lo ya guardado.')
            print('     Para leerlo ahora, con Firefox:  python fb.py detalle %d )' % p['id'])
        else:
            try:
                tot = int(re.sub(r'\D', '', str(p['reacciones'] or '')) or 0)
            except Exception:
                tot = 0
            print('    Personas leídas: %d reaccionaron%s, %d compartieron' % (len(rs), (' (Facebook indica %d)' % tot) if tot else '', len(ss)))
            if 'detalle_limitado' in p.keys() and p['detalle_limitado']:
                print(envolver('AVISO: Facebook NO da la lista completa de esta publicación (suele pasar en páginas con muchas reacciones): '
                               'solo muestra los nombres de TUS AMIGOS que reaccionaron, aunque sigas la página. Es un límite de Facebook, no un fallo.'
                               + (' Totales por reacción: %s.' % p['detalle_limitado'] if p['detalle_limitado'] != 'limitada' else '')))
            por = {}
            for r in rs:
                por.setdefault(r['reaccion'] or '?', []).append(r)
            for emo, lst in por.items():
                if 3 in OPC['enlaces']:
                    print('    %s (%d):' % (emo, len(lst)))
                    for r in lst:
                        print('      %s  %s' % (r['nombre'], url_perfil(r['url'])))
                else:
                    print(textwrap.fill('%s (%d): %s' % (emo, len(lst), ', '.join(r['nombre'] for r in lst)), width=ancho(),
                                        initial_indent='    ', subsequent_indent='      '))
            if ss:
                if 3 in OPC['enlaces']:
                    print('    Compartida por (%d):' % len(ss))
                    for r in ss:
                        print('      %s  %s' % (r['nombre'], url_perfil(r['url'])))
                else:
                    print(textwrap.fill('Compartida por (%d): %s' % (len(ss), ', '.join(r['nombre'] for r in ss)), width=ancho(),
                                        initial_indent='    ', subsequent_indent='      '))
            if not rs and not ss and not ('detalle_limitado' in p.keys() and p['detalle_limitado']):
                print('    (el detalle se consultó el %s pero no devolvió personas)' % p['detalle_vez'][:16])
    if com:
        cs = con.execute('SELECT * FROM comentarios WHERE pub_id=? ORDER BY id', (p['id'],)).fetchall()
        if cs:
            print('    Comentarios:')
            for c in cs:
                tag = ' [NUEVO]' if c['visto'] == 0 else ''
                t = corto_tiempo(c['tiempo_texto']).replace('hace ', '')
                pref = '        > ' if c['respuesta'] else '      - '
                txt = '%s%s%s: %s' % (c['autor'] or '?', (' (%s)' % t) if t else '', tag, (c['texto'] or '').replace('\n', ' '))
                print(textwrap.fill(txt, width=ancho(), initial_indent=pref, subsequent_indent=' ' * len(pref) + '  '))
                if 2 in OPC['enlaces'] and c['autor_url']:
                    print(' ' * len(pref) + '  perfil: ' + url_perfil(c['autor_url']))
                nrc = c['reacciones'] if 'reacciones' in c.keys() else 0
                if nrc:
                    rcs = con.execute('SELECT nombre, url, reaccion FROM reac_comentarios WHERE com_id=? ORDER BY reaccion, nombre', (c['id'],)).fetchall()
                    sang = ' ' * len(pref) + '  '
                    if not rcs:
                        print('%sreacciones: %s%s' % (sang, nrc, ' (Facebook solo muestra a tus amigos)' if 'lista_limitada' in c.keys() and c['lista_limitada'] else ''))
                    elif 3 in OPC['enlaces']:
                        print('%sreacciones: %s (leídas %d)' % (sang, nrc, len(rcs)))
                        for r in rcs:
                            print('%s  %s %s  %s' % (sang, r['reaccion'] or '?', r['nombre'], url_perfil(r['url'])))
                    else:
                        print(textwrap.fill('reacciones: %s (%s)' % (nrc, ', '.join('%s %s' % (r['reaccion'] or '?', r['nombre']) for r in rcs)), width=ancho(),
                                            initial_indent=sang, subsequent_indent=sang + '  '))
    if marcar:
        con.execute('UPDATE publicaciones SET visto=1 WHERE id=?', (p['id'],))
        con.execute('UPDATE comentarios SET visto=1 WHERE pub_id=?', (p['id'],))
    print()


def fechas_inferidas(filas, ancla=False):
    """Para publicaciones SIN fecha: si la anterior y la siguiente (en el orden de la página, misma lectura) son del mismo día,
    la propia es de ese día (las páginas van de la más reciente a la más antigua). Devuelve {id: 'AAAA-MM-DD'}."""
    grupos = {}
    for p in filas:
        grupos.setdefault(p['captura_id'], []).append(p)
    out = {}
    for g in grupos.values():
        g.sort(key=lambda p: (p['orden'] if p['orden'] is not None else 0))
        for i, p in enumerate(g):
            if (p['fecha_aprox'] or '')[:10]:
                continue
            a = next(((q['fecha_aprox'] or '')[:10] for q in reversed(g[:i]) if (q['fecha_aprox'] or '')[:10]), '')
            b = next(((q['fecha_aprox'] or '')[:10] for q in g[i + 1:] if (q['fecha_aprox'] or '')[:10]), '')
            if a and a == b:
                out[p['id']] = a
            elif ancla and (a or b):   # solo para ordenar: se coloca justo después de la anterior con fecha
                out[p['id']] = a or b
    return out


def posts_de(con, fid, solo_nuevas=False, limite=0, buscar=None):
    q = 'SELECT p.* FROM publicaciones p WHERE p.fuente_id=?'
    par = [fid]
    if solo_nuevas:
        q += ' AND ' + _post_nuevo_sql()
    if buscar:
        q += ' AND (lower(p.texto) LIKE ? OR lower(p.autor) LIKE ?)'
        par += ['%' + buscar.lower() + '%'] * 2
    # primero lo no visto (nuevas publicaciones y luego las que solo tienen comentarios nuevos),
    # dentro de cada grupo lo más reciente primero y, en una misma captura, en el orden de la página
    filas = con.execute(q, par).fetchall()
    inf = fechas_inferidas(filas, ancla=True)
    nuevos_com = {r[0] for r in con.execute('SELECT DISTINCT pub_id FROM comentarios WHERE visto=0').fetchall()}
    # primero lo no visto (nuevas publicaciones y luego las que solo tienen comentarios nuevos); dentro de cada grupo,
    # de la más reciente a la más antigua por FECHA (las que no traen fecha, por su posición en la página)
    def clave(p):
        f = (p['fecha_aprox'] or '')[:16] or inf.get(p['id'], '')
        return (0 if p['visto'] == 0 else 1, 0 if p['id'] in nuevos_com else 1, '' if f else '0', f, -(p['captura_id'] or 0), p['orden'] or 0)
    # fecha descendente: se ordena en dos pasos (estable)
    filas.sort(key=lambda p: (-(p['captura_id'] or 0), p['orden'] or 0))
    filas.sort(key=lambda p: (clave(p)[3] or ''), reverse=True)
    filas.sort(key=lambda p: (clave(p)[0], clave(p)[1]))
    if limite:
        filas = filas[:limite]
    return filas


def cmd_ver(a):
    con = db()
    f = buscar_fuente(con, a.fuente)
    total = con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (f['id'],)).fetchone()[0]
    nuevas = con.execute('SELECT COUNT(*) FROM publicaciones p WHERE fuente_id=? AND ' + _post_nuevo_sql(), (f['id'],)).fetchone()[0]
    print('=' * ancho())
    print('%s  (%s)' % (f['nombre'], f['tipo']))
    print('Capturas: %d   última: %s   publicaciones: %d   con novedades: %d' % (f['n_capturas'], (f['ultima_captura'] or '-')[:16], total, nuevas))
    if f['url']:
        print(f['url'])
    print('=' * ancho() + '\n')
    ps = posts_de(con, f['id'], a.solo_nuevas, a.n)
    if not ps:
        print('No hay publicaciones%s.' % (' nuevas' if a.solo_nuevas else ''))
    for p in ps:
        mostrar_post(con, p, marcar=not a.no_marcar, com=not a.sin_comentarios)
    con.commit()
    if len(ps) < total and not a.solo_nuevas:
        print('(mostradas %d de %d; usa -n 0 para todas)' % (len(ps), total))


def cmd_nuevas(a):
    con = db()
    hay = False
    for f in con.execute('SELECT * FROM fuentes ORDER BY id').fetchall():
        ps = posts_de(con, f['id'], True, a.n)
        ncom = con.execute('SELECT COUNT(*) FROM comentarios c JOIN publicaciones p ON p.id=c.pub_id WHERE p.fuente_id=? AND c.visto=0', (f['id'],)).fetchone()[0]
        if not ps:
            continue
        hay = True
        if a.resumen:
            print('%-4s %-40s %3d con novedades, %d comentarios nuevos' % (f['id'], f['nombre'][:40], len(ps), ncom))
            continue
        print('#' * ancho())
        print('%s  (%d con novedades)' % (f['nombre'], len(ps)))
        print('#' * ancho() + '\n')
        for p in ps:
            mostrar_post(con, p, marcar=not a.no_marcar)
    con.commit()
    if not hay:
        print('No hay novedades pendientes de ver.')


def cmd_buscar(a):
    con = db()
    like = '%' + a.texto.lower() + '%'
    q = """SELECT p.id, f.nombre, p.autor, p.fecha_aprox, p.primera_vez, p.texto FROM publicaciones p
           JOIN fuentes f ON f.id=p.fuente_id WHERE (lower(p.texto) LIKE ? OR lower(p.texto_compartido) LIKE ? OR lower(p.autor) LIKE ?
           OR EXISTS(SELECT 1 FROM comentarios c WHERE c.pub_id=p.id AND lower(c.texto) LIKE ?))"""
    par = [like] * 4
    if a.fuente:
        f = buscar_fuente(con, a.fuente)
        q += ' AND f.id=?'
        par.append(f['id'])
    q += ' ORDER BY p.captura_id DESC, p.orden LIMIT ?'
    par.append(a.n or 1000)
    filas = con.execute(q, par).fetchall()
    if not filas:
        print('Sin resultados.')
        return
    print(tabla(['Nº', 'Fuente', 'Autor', 'Fecha', 'Texto'],
                [[r['id'], r['nombre'], r['autor'], (r['fecha_aprox'] or r['primera_vez'])[:16], (r['texto'] or '').replace('\n', ' ')[:60]] for r in filas]))
    print('\n(usa "python fb.py post Nº" para leer una publicación completa)')


CAT_LISTA = {'pagina': 'paginas', 'grupo': 'grupos', 'contacto': 'contactos', 'siguiendo': 'personas', 'amigo': 'amigos'}
CAT_FUENTE = {'grupo': 'grupos', 'pagina': 'paginas', 'persona_grupo': 'personas', 'inicio': 'inicio'}
CATEGORIAS = ('paginas', 'grupos', 'contactos', 'personas', 'amigos', 'inicio', 'mensajes', 'otras')


def categorias_fuentes(con):
    """Para cada fuente, a qué listas pertenece (páginas, grupos, contactos, personas, amigos)."""
    mapa = {}
    for l in con.execute('SELECT tipo, clave FROM listas').fetchall():
        cat = CAT_LISTA.get(l['tipo'])
        if not cat:
            continue
        mapa.setdefault(l['clave'], set()).add(cat)
        if l['clave'].startswith('msg-'):
            mapa.setdefault('id-' + l['clave'][4:], set()).add(cat)
    res = {}
    for f in con.execute('SELECT id, clave, tipo FROM fuentes').fetchall():
        res[f['id']] = set(mapa.get(f['clave']) or {CAT_FUENTE.get(f['tipo'], 'otras')})
    return res


def cmd_ultimas(a):
    """Las últimas N publicaciones, de una fuente o mezcladas de todas, por fecha."""
    con = db()
    de = None
    if a.de:
        de = {x.strip().lower() for x in a.de.split(',') if x.strip()}
        malas = de - set(CATEGORIAS)
        if malas:
            sys.exit('Categoría desconocida: %s.  Usa: %s' % (', '.join(sorted(malas)), ', '.join(CATEGORIAS)))
    f = buscar_fuente(con, a.fuente) if a.fuente else None
    cats = categorias_fuentes(con)
    q = 'SELECT p.*, f.nombre AS fnombre FROM publicaciones p JOIN fuentes f ON f.id=p.fuente_id'
    par = []
    if f:
        q += ' WHERE p.fuente_id=?'
        par.append(f['id'])
    items = []
    filas_u = con.execute(q, par).fetchall()
    inf_u = fechas_inferidas(filas_u, ancla=True)
    for p in filas_u:
        if de and not (cats.get(p['fuente_id'], set()) & de):
            continue
        items.append((p['fecha_aprox'] or inf_u.get(p['id']) or (p['primera_vez'] or '')[:16], 'p', p))
    if not f and (not de or 'mensajes' in de):
        for m in con.execute('SELECT * FROM mensajes').fetchall():
            items.append((m['primera_vez'][:16], 'm', m))
    items.sort(key=lambda x: (x[0], x[2]['id']), reverse=True)
    sel = items[:a.n] if a.n else items
    if getattr(a, 'detalle', False):   # leer en Facebook quién reaccionó/compartió de estas publicaciones antes de mostrarlas
        ids_ = [r['id'] for _, t, r in sel if t == 'p' and r['permalink']]
        if ids_:
            con.close()
            cmd_detalle(argparse.Namespace(ids=ids_, ultimas=None, de=None, releer=getattr(a, 'releer', False), foto=False, abrir=False, cola=False,
                                           espera=240, pausa=3, navegador=getattr(a, 'navegador', None)))
            con = db()
            OPC['reacciones'] = True
    print('=' * ancho())
    print('ÚLTIMAS %d %s%s   (más reciente primero)' % (len(sel), ('de ' + f['nombre']) if f else 'de todo lo capturado',
                                                          (' [' + ', '.join(sorted(de)) + ']') if de else ''))
    print('=' * ancho() + '\n')
    if not sel:
        print('No hay nada capturado con ese criterio.')
    for _, tipo, r in sel:
        if tipo == 'p':
            mostrar_post(con, r, marcar=not a.no_marcar, com=not a.sin_comentarios, fnombre=r['fnombre'])
        else:
            print('%s[MENSAJE]  %s  -  captado %s' % ('[NUEVO] ' if r['visto'] == 0 else '', r['conv_nombre'] or r['conv'], r['primera_vez'][:16]))
            print(envolver(r['texto'], '    '))
            print()
            if not a.no_marcar:
                con.execute('UPDATE mensajes SET visto=1 WHERE id=?', (r['id'],))
    con.commit()
    if any(t == 'm' for _, t, _ in sel):
        print('(los mensajes no traen fecha propia: se ordenan por el momento en que se captaron)')


# ----------------------------------------------------------------------------
# publicaciones de un día o de varios días (Facebook, X e Instagram)
# ----------------------------------------------------------------------------

DIAS_SEM = {'lunes': 0, 'martes': 1, 'miercoles': 2, 'jueves': 3, 'viernes': 4, 'sabado': 5, 'domingo': 6}


def parse_dia(txt, hoy=None):
    """'hoy', 'ayer', 'anteayer', 'lunes'..'domingo' (el más reciente), 2026-10-09, 9/10/2026, 9/10, 'hace 3 dias' -> date."""
    hoy = hoy or datetime.now().date()
    t = sin_acentos((txt or '').strip().lower())
    if t in ('hoy', 'today'):
        return hoy
    if t in ('ayer', 'yesterday'):
        return hoy - timedelta(days=1)
    if t == 'anteayer':
        return hoy - timedelta(days=2)
    if t in DIAS_SEM:
        return hoy - timedelta(days=(hoy.weekday() - DIAS_SEM[t]) % 7)
    m = re.match(r'^hace\s+(\d+)\s*d', t)
    if m:
        return hoy - timedelta(days=int(m.group(1)))
    try:
        m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', t)
        if m:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        m = re.match(r'^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$', t)
        if m:
            y = int(m.group(3)) if m.group(3) else hoy.year
            y += 2000 if y < 100 else 0
            d = datetime(y, int(m.group(2)), int(m.group(1))).date()
            if not m.group(3) and d > hoy:
                d = d.replace(year=y - 1)
            return d
    except ValueError:
        pass
    sys.exit('Fecha no válida: "%s". Usa por ejemplo: 2026-10-09   9/10/2026   9/10   hoy   ayer   sabado   hace 3 dias' % txt)


def rangos_de(a):
    """Lista de (desde, hasta) como date, a partir de --dia (uno o varios), --desde/--hasta y --ultimos-dias."""
    hoy = datetime.now().date()
    rs = []
    for d in (getattr(a, 'dia', None) or []):
        x = parse_dia(d, hoy)
        rs.append((x, x))
    desde = getattr(a, 'desde', None)
    hasta = getattr(a, 'hasta', None)
    if desde or hasta:
        d1 = parse_dia(desde, hoy) if desde else datetime(2000, 1, 1).date()
        d2 = parse_dia(hasta, hoy) if hasta else hoy
        rs.append((min(d1, d2), max(d1, d2)))
    nu = getattr(a, 'ultimos_dias', None)
    if nu:
        rs.append((hoy - timedelta(days=nu - 1), hoy))
    if not rs:
        sys.exit('Indica el día o los días:  --dia 2026-10-09 [otro día ...]   |   --desde 5/10 --hasta 9/10   |   --ultimos-dias 3')
    return rs


def texto_rangos(rs):
    return ', '.join(('%s' % d1) if d1 == d2 else ('%s a %s' % (d1, d2)) for d1, d2 in rs)


def fecha_de_post(con, p):
    """Fecha (date) de una publicación: la suya o, si no la trae (miniaturas de Instagram), la de otra lectura del mismo enlace."""
    f = (p['fecha_aprox'] or '')[:10]
    if not f and p['permalink']:
        m = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', p['permalink'])
        if m:
            r = con.execute("SELECT fecha_aprox FROM publicaciones WHERE permalink LIKE ? AND COALESCE(fecha_aprox,'')<>'' LIMIT 1", ('%/' + m.group(1) + '%',)).fetchone()
        else:
            r = con.execute("SELECT fecha_aprox FROM publicaciones WHERE permalink=? AND COALESCE(fecha_aprox,'')<>'' LIMIT 1", (p['permalink'],)).fetchone()
        f = (r[0] or '')[:10] if r else ''
    try:
        return datetime.strptime(f, '%Y-%m-%d').date() if f else None
    except ValueError:
        return None


def posts_en_rangos(con, fids, rangos, filtro=None, unico=None):
    """(publicaciones dentro de los rangos [(fecha, post)] de la más reciente a la más antigua, nº sin fecha, fecha más antigua guardada).
    La "más antigua" es la segunda más antigua (si hay 3 o más): así una publicación fijada de hace meses no engaña.
    filtro(p) -> bool descarta publicaciones; unico(p) -> clave para quedarse con una sola copia (la que trae fecha y texto completo)."""
    q = 'SELECT p.*, f.nombre AS fnombre FROM publicaciones p JOIN fuentes f ON f.id=p.fuente_id'
    par = []
    if fids is not None:
        if not fids:
            return [], 0, None
        q += ' WHERE p.fuente_id IN (%s)' % ','.join('?' * len(fids))
        par = list(fids)
    filas = [p for p in con.execute(q, par).fetchall() if not filtro or filtro(p)]
    if unico:
        mejor = {}
        for p in filas:
            k = unico(p)
            def valor(q):
                return (bool(q['fecha_aprox']), not (q['texto'] or '').startswith('[miniatura]'), len(q['texto'] or ''))
            if k not in mejor or valor(p) > valor(mejor[k]):
                mejor[k] = p
        filas = list(mejor.values())
    dentro, sin, fechas = [], 0, []
    inf = fechas_inferidas(filas)
    for p in filas:
        d = fecha_de_post(con, p)
        if d is None and p['id'] in inf:
            try:
                d = datetime.strptime(inf[p['id']], '%Y-%m-%d').date()
            except ValueError:
                d = None
        if d is None:
            sin += 1
            continue
        fechas.append(d)
        if any(d1 <= d <= d2 for d1, d2 in rangos):
            dentro.append((d, p))
    fechas.sort()
    viejo = (fechas[1] if len(fechas) >= 3 else fechas[0]) if fechas else None
    dentro.sort(key=lambda x: ((x[1]['fecha_aprox'] or str(x[0])), x[1]['id']), reverse=True)
    return dentro, sin, viejo


def estado_lectura(con, fids, filtro=None, unico=None):
    """(nº de publicaciones guardadas, fecha más antigua) de esas fuentes."""
    if not fids:
        return 0, None
    _, _, viejo = posts_en_rangos(con, fids, [(datetime(2000, 1, 1).date(), datetime(2100, 1, 1).date())], filtro, unico)
    n = con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id IN (%s)' % ','.join('?' * len(fids)), list(fids)).fetchone()[0]
    return n, viejo


def estado_ultima_lectura(con, fids, filtro=None):
    """(nº de publicaciones de la ÚLTIMA lectura, fecha más antigua DE ESA LECTURA). Que existan publicaciones antiguas guardadas de
    otras lecturas no significa que se hayan leído todas las intermedias: solo cuenta hasta dónde llegó esta lectura."""
    if not fids:
        return 0, None
    marcas = ','.join('?' * len(fids))
    t = con.execute('SELECT MAX(fecha) FROM capturas WHERE fuente_id IN (%s)' % marcas, list(fids)).fetchone()[0]
    if not t:
        return 0, None
    filas = [p for p in con.execute('SELECT * FROM publicaciones WHERE fuente_id IN (%s) AND ultima_vez=?' % marcas, list(fids) + [t]).fetchall()
             if not filtro or filtro(p)]
    fechas = sorted(d for d in (fecha_de_post(con, p) for p in filas) if d)
    viejo = (fechas[1] if len(fechas) >= 3 else fechas[0]) if fechas else None
    return len(filas), viejo


def leer_hasta_fecha(abrir, estado_fn, d_min, inicio=20, tope=150, devueltas_fn=None):
    """Repite la lectura pidiendo cada vez más publicaciones hasta pasar de d_min.
    abrir(n) -> (ok, detalle).  estado_fn() -> (nº guardadas, fecha más antigua).
    devueltas_fn() -> nº de publicaciones que trajo la ÚLTIMA lectura: si trae menos de las pedidas, la página no tiene más.
    (Que no haya publicaciones NUEVAS no significa que se haya acabado: pueden ser las mismas ya guardadas.)"""
    n = max(1, inicio)
    prev = -1
    hoy = datetime.now().date()
    while True:
        print('--- Leyendo hasta %d publicaciones...' % n)
        ok, det = abrir(n)
        if not ok:
            print('    No se pudo leer (%s).' % det)
            return False
        cnt, viejo = estado_fn()
        dev = devueltas_fn() if devueltas_fn else None
        print('    Guardadas: %d%s. La más antigua es del %s.' % (cnt, (' (esta lectura trajo %d)' % dev) if dev is not None else '', viejo or '(sin fecha)'))
        if viejo and viejo < d_min:
            print('    Ya se llegó a antes del %s: se tienen todas las de esos días (las que la página cargó).' % d_min)
            return True
        if dev is not None:
            if dev < n * 0.8:
                print('    La página no da más publicaciones (pedí %d y trajo %d). Se muestra lo que hay.' % (n, dev))
                return True
        elif cnt <= prev:
            print('    No carga más publicaciones (la página no tiene más o dejó de cargar). Se muestra lo que hay.')
            return True
        if n >= tope:
            print('    Tope de %d publicaciones alcanzado (opción --max).' % tope)
            return True
        prev = cnt
        sig = n * 2
        if viejo and viejo < hoy:   # estimar cuántas hacen falta según el ritmo visto (publicaciones por día)
            ritmo = n / max(1, (hoy - viejo).days)
            sig = max(sig, int(ritmo * max(1, (hoy - d_min).days) * 1.25) + 1)
            print('    Ritmo aproximado: %.0f publicaciones al día; próxima lectura: %d.' % (ritmo, min(tope, sig)))
        n = min(tope, sig)


def leer_rango(url, nombre, rangos, a, pkg):
    """Busca los días pedidos POR FASES: cada fase abre la página, la extensión avanza publicación a publicación (esperando a que
    Facebook dibuje la fecha), guarda lo leído en Termux sobre la marcha y deja un marcador de hasta dónde llegó; si la fase se
    corta (límite, desconexión), la siguiente retoma desde ese marcador en vez de empezar de cero."""
    d1 = min(x for x, _ in rangos)
    d2 = max(y for _, y in rangos)
    d2d = d2 if isinstance(d2, date) else datetime.strptime(str(d2)[:10], '%Y-%m-%d').date()
    dn2 = (d2d - date(1970, 1, 1)).days
    fase_n = max(10, int(getattr(a, 'fase', 80) or 80))
    total = max(fase_n, a.max)
    print('--- [fb.py %s] Buscando en %s las publicaciones del %s (por fases de %d publicaciones, hasta %d en total)...' % (VERSION, nombre, texto_rangos(rangos), fase_n, total))
    ck = clave_de_url(url.split('#')[0])
    visto = 0
    ok, det = False, ''
    hubo_marcador = False
    for fase in range(1, 31):
        h = '#fbv-rango=%s..%s' % (d1, d2) + ('&fbv-releer=1' if getattr(a, 'releer', False) else '')
        c = db()
        try:
            cu = c.execute('SELECT * FROM cursores WHERE fuente=?', (ck,)).fetchone()
        finally:
            c.close()
        if cu and cu['dn'] and cu['dn'] >= dn2 and delta_seg(cu['actualizada']) < 3 * 86400:
            h += '&fbv-cursor=%s~%d' % (quote(cu['clave'], safe=''), cu['dn'])
            hubo_marcador = True
            print('  fase %d: retomo desde la última publicación leída (%s)' % (fase, (date(1970, 1, 1) + timedelta(days=cu['dn'])).isoformat()))
        else:
            print('  fase %d: empiezo desde arriba' % fase)
        res = lanzar_items([(url.split('#')[0] + h, 'capturar', None, min(fase_n, total - visto), nombre)], pkg, a.espera, a.pausa, volver=False)
        ok, det = res[0][1] == 'hecha', res[0][2]
        m = re.search(r'(\d+) publicaciones vistas', det or '')
        visto += int(m.group(1)) if m else fase_n
        final = any(t in (det or '') for t in ('pasado el rango', 'no cargan más', 'sin fecha legible'))
        if final:
            c = db()
            c.execute('DELETE FROM cursores WHERE fuente=?', (ck,))
            c.commit()
            c.close()
            break
        c = db()
        try:
            cu2 = c.execute('SELECT * FROM cursores WHERE fuente=?', (ck,)).fetchone()
        finally:
            c.close()
        avanzo = bool(cu2) and (not cu or cu2['clave'] != cu['clave'])
        if not avanzo or visto >= total:
            if not avanzo:
                print('  (la fase no avanzó: paro; vuelve a lanzar el mismo comando y retomará desde el marcador)')
            break
        print('  fase %d terminada (%s); sigo con la siguiente...' % (fase, (det or '')[:90]))
    return ok, det


def url_rapida(url):
    """Añade a la URL (tras #) cuántas publicaciones de esa fuente ya hay guardadas: la extensión pasa por encima de ese número
    de publicaciones sin abrir sus comentarios (para llegar antes a las más antiguas)."""
    base = url.split('#')[0]
    c = db()
    try:
        ff = fuente_por_clave(c, clave_de_url(base))
        n = c.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (ff['id'],)).fetchone()[0] if ff else 0
    finally:
        c.close()
    return base + ('#fbv-rapido=%d' % n if n > 0 else '')


def n_ultima_captura(url):
    """Publicaciones que trajo la última lectura de esa fuente."""
    c = db()
    try:
        ff = fuente_por_clave(c, clave_de_url(url))
        if not ff:
            return None
        r = c.execute('SELECT n_posts FROM capturas WHERE fuente_id=? ORDER BY id DESC LIMIT 1', (ff['id'],)).fetchone()
        return r['n_posts'] if r else None
    finally:
        c.close()


def mostrar_dia(con, fids, rangos, a, nombre, filtro=None, com=True, aprox=True, leido=False, unico=None):
    """Muestra las publicaciones guardadas de esos días (de las fuentes fids, o de todo si fids es None)."""
    dentro, sin, viejo = posts_en_rangos(con, fids, rangos, filtro, unico)
    print('=' * ancho())
    print('PUBLICACIONES DEL %s   en %s: %d' % (texto_rangos(rangos), nombre, len(dentro)))
    print('=' * ancho())
    if not dentro:
        print('No hay ninguna guardada de esos días.')
    for d, p in dentro:
        mostrar_post(con, p, marcar=not getattr(a, 'no_marcar', False), com=com and not getattr(a, 'sin_comentarios', False), fnombre=p['fnombre'])
    con.commit()
    d_min = min(d1 for d1, _ in rangos)
    if viejo and viejo >= d_min:
        print('\n(!) Lo guardado solo llega hasta el %s y tú pides desde el %s: puede faltar lo de esos días.' % (viejo, d_min))
        if not leido:
            print('    Para leerlo hasta ahí:  añade --leer')
    elif not viejo and not dentro and not leido:
        print('\n(!) Aún no hay nada guardado de esta fuente con fecha. Añade --leer para leerla ahora.')
    if sin:
        print('\n(%d publicaciones guardadas no traen fecha y no se han podido incluir.)' % sin)
    if aprox:
        print('\nLas fechas de Facebook son aproximadas (Facebook solo dice "hace 3 d"): puede haber un día de diferencia.')


def filtros_busqueda(recientes, anio):
    """Valor del parámetro filters= de Facebook (JSON en base64) para «Más recientes» y/o un año."""
    import base64
    d = {}
    if recientes:
        d['rp_chrono_sort:0'] = json.dumps({'name': 'chronosort', 'args': ''}, separators=(',', ':'))
    if anio:
        y = str(int(anio))
        args = json.dumps({'start_year': y, 'start_month': y + '-1', 'end_year': y, 'end_month': y + '-12',
                           'start_day': y + '-1-1', 'end_day': y + '-12-31'}, separators=(',', ':'))
        d['rp_creation_time:0'] = json.dumps({'name': 'creation_time', 'args': args}, separators=(',', ':'))
    if not d:
        return ''
    return quote(base64.b64encode(json.dumps(d, separators=(',', ':')).encode()).decode(), safe='')


def fuente_de_url_crear(con, url):
    """La fuente de una URL de página/grupo; si aún no existe se crea SIN vigilar."""
    ck = clave_de_url(url)
    if not ck:
        return None
    f = fuente_por_clave(con, ck)
    if f:
        return f
    base = url_sin_busqueda(url).split('?')[0]
    nombre = nombre_desde_listas(con, ck, base) or ck
    cur = con.execute('INSERT INTO fuentes(clave,tipo,nombre,url,creada,vigilada,bloqueada) VALUES(?,?,?,?,?,0,0)',
                      (ck, 'grupo' if ck.startswith('grupo-') else 'pagina', nombre, base, ahora()))
    con.commit()
    return con.execute('SELECT * FROM fuentes WHERE id=?', (cur.lastrowid,)).fetchone()


def cmd_buscaren(a):
    """Busca un término DENTRO de una página o de un grupo (su buscador propio) y muestra lo que sale.
    Dos formas:  buscaren G1 término    |    buscaren G1 "https://www.facebook.com/groups/ID/search?q=...&filters=..."   (o solo la URL)."""
    con = db()
    t_ini = ahora()
    fuente, termino = a.fuente, a.termino
    if re.match(r'^https?://', fuente or '') and not termino:   # solo la URL
        fuente, termino = None, a.fuente
    if not termino:
        sys.exit('Falta el término a buscar.  Ejemplo:  python3 fb.py buscaren G1 habitacion -n 20')
    f = None
    if fuente:
        if REF_RE.match(fuente):
            l = lista_por_ref(con, fuente)
            f = fuente_de_lista(con, l) or fuente_de_ref_crear(con, l)
        else:
            f = buscar_fuente(con, fuente)
    esurl = termino.lower().startswith('http')
    if esurl:                       # URL copiada del navegador tras aplicar filtros (año, recientes...)
        url = termino
        term = (parse_qs(urlparse(url).query).get('q') or [''])[0]
        if not f:
            f = fuente_de_url_crear(con, url)
        if not f:
            sys.exit('No reconozco de qué página o grupo es esa URL.')
    else:
        term = termino
        if not f:
            sys.exit('No encuentro esa fuente. Mira  python3 fb.py listas grupos   o   listas paginas.')
        ck = f['clave'] or ''
        if ck.startswith('grupo-'):
            base = 'https://www.facebook.com/groups/%s/search' % ck[6:]
        elif ck.startswith('id-'):
            base = 'https://www.facebook.com/profile/%s/search' % ck[3:]
        elif ck and not ck.startswith(('otro', 'post-', 'msg-')):
            base = 'https://www.facebook.com/%s/search' % ck
        else:
            sys.exit('Esa fuente no es una página ni un grupo con buscador.')
        url = base + '?q=' + quote(term)
        flt = filtros_busqueda(a.recientes, a.anio)
        if flt:
            url += '&filters=' + flt
    pkg = paquete_navegador(a)
    print('Busco "%s" en %s (%d publicaciones como máximo):\n   %s' % (term or termino, f['nombre'], a.n, url))
    con.commit()
    con.close()
    asegurar_servidor()
    res = lanzar_items([(url, 'capturar', None, a.n, f['nombre'])], pkg, a.espera, 0, volver=not a.sin_volver)
    if not res or res[0][1] != 'hecha':
        sys.exit('No se pudo leer la búsqueda (%s).' % (res[0][2] if res else '?'))
    con = db()
    f = con.execute('SELECT * FROM fuentes WHERE id=?', (f['id'],)).fetchone()
    # lo que salió de ESTA búsqueda: lo capturado ahora (Facebook también devuelve publicaciones que mencionan el término
    # solo en los comentarios, o con otras palabras, así que no se filtra por texto)
    filas = con.execute('SELECT * FROM publicaciones WHERE fuente_id=? AND ultima_vez>=? ORDER BY captura_id DESC, orden', (f['id'], t_ini)).fetchall()
    print('\n' + '=' * ancho())
    print('RESULTADOS DE "%s" EN %s: %d publicaciones (en el orden en que las devuelve Facebook)' % (term or termino, f['nombre'], len(filas)))
    print('=' * ancho() + '\n')
    for p in filas[:a.n]:
        mostrar_post(con, p, marcar=False, com=False, fnombre=f['nombre'])
    if not filas:
        print('No se leyó ninguna publicación. Mira  python3 fb.py cola  y, si la página cargó, envíame el diagnóstico de la extensión.')
    con.commit()


def cmd_alimentar(a):
    """Lee (guarda) las publicaciones de una o varias páginas/grupos de una fecha o intervalo de fechas, sin mostrarlas."""
    rangos = rangos_de(a)
    d_min = min(d1 for d1, _ in rangos)
    con = db()
    objetivos = []   # (url, fuente o None, nombre)
    vistos = set()

    def anadir(url, f, nombre):
        k = norm_url(url) if url else ''
        if url and k not in vistos:
            vistos.add(k)
            objetivos.append((url, f, nombre))

    if a.fuentes:
        for x in a.fuentes:
            if REF_RE.match(x):
                l = lista_por_ref(con, x)
                u = url_de_lista(l)
                f = fuente_de_lista(con, l)
                if u:
                    anadir(u, f, '%s %s' % (x.upper(), l['nombre']))
                else:
                    print('%s no tiene página que abrir.' % x.upper())
            else:
                f = buscar_fuente(con, x, silencioso=True)
                if f:
                    anadir(f['url'], f, f['nombre'])
                elif re.search(r'/|\.', x):
                    anadir(expandir_url(x), None, x[:50])
                else:
                    buscar_fuente(con, x)
    else:
        de = {y.strip().lower() for y in a.de.split(',') if y.strip()} if a.de else None
        cats = categorias_fuentes(con) if de else {}
        usadas = set()
        for f in con.execute("SELECT * FROM fuentes WHERE vigilada=1 AND bloqueada=0 AND url<>'' ORDER BY id").fetchall():
            if f['id'] in usadas or (de and not (cats.get(f['id'], set()) & de)):
                continue
            usadas |= misma_fuente(con, f)
            anadir(f['url'], f, f['nombre'])
    if not objetivos:
        sys.exit('No hay nada que leer: indica páginas/grupos (P141 G2 URL...) o vigila alguna.')
    con.close()
    asegurar_servidor()
    pkg = paquete_navegador(a)
    print('Alimento %d fuente(s) con las publicaciones de %s (leyendo hacia atrás hasta llegar a %s):' % (len(objetivos), texto_rangos(rangos), d_min))
    resumen = []
    for k, (url, f, nombre) in enumerate(objetivos, 1):
        print('\n[%d/%d] %s' % (k, len(objetivos), nombre))

        def estado(url=url):
            c = db()
            ff = fuente_por_clave(c, clave_de_url(url))
            r = estado_lectura(c, [ff['id']] if ff else [])
            c.close()
            return r
        ok, det = leer_rango(url, nombre, rangos, a, pkg)
        if not ok:
            print('    No se pudo leer (%s).' % det)
        resumen.append((nombre, url, True))
    volver_a_termux()
    con = db()
    print('\n' + '=' * ancho())
    print('RESUMEN: publicaciones guardadas de %s' % texto_rangos(rangos))
    print('=' * ancho())
    for nombre, url, _ in resumen:
        ff = fuente_por_clave(con, clave_de_url(url))
        if not ff:
            print('   %-40s (no se guardó nada)' % nombre[:40])
            continue
        dentro, sin, viejo = posts_en_rangos(con, [ff['id']], rangos)
        print('   %-40s %3d en esas fechas   (guardado hasta %s%s)' % (nombre[:40], len(dentro), viejo or 'sin fecha',
                                                                      ', %d sin fecha' % sin if sin else ''))
    print('\nVerlas:  python fb.py dia FUENTE  con las mismas fechas (--dia / --desde / --hasta)')


def cmd_dia(a):
    """Publicaciones guardadas de un día o grupo de días, de una fuente o de todo; con --leer las lee antes hasta llegar a esa fecha."""
    rangos = rangos_de(a)
    con = db()
    f = None
    url = None
    if a.fuente:
        if REF_RE.match(a.fuente):
            l = lista_por_ref(con, a.fuente)
            url = url_de_lista(l)
            f = fuente_de_lista(con, l)
        else:
            f = buscar_fuente(con, a.fuente, silencioso=True)
            if not f and re.search(r'/|\.', a.fuente):
                url = expandir_url(a.fuente)
            elif not f:
                buscar_fuente(con, a.fuente)
        if f and not url:
            url = f['url']
    if a.leer:
        if not url:
            sys.exit('--leer necesita una fuente con dirección: indica la fuente, una referencia (P141) o la URL.')
        d_min = min(d1 for d1, _ in rangos)
        pkg = paquete_navegador(a)
        nombre = f['nombre'] if f else url[:50]
        con.close()
        asegurar_servidor()

        ok, det = leer_rango(url, nombre, rangos, a, pkg)
        if not ok:
            print('    No se pudo leer (%s).' % det)
        volver_a_termux()
        con = db()
        f = fuente_por_clave(con, clave_de_url(url)) or f
    fids = [f['id']] if f else None
    mostrar_dia(con, fids, rangos, a, f['nombre'] if f else 'todo lo guardado', leido=a.leer)


# ----------------------------------------------------------------------------
# seguir un pueblo o ciudad: páginas, grupos y búsquedas guardadas bajo un nombre
# ----------------------------------------------------------------------------

def lugares_cargar():
    return dict(config().get('lugares') or {})


def lugares_guardar(d):
    try:
        c = json.load(open(CFG, encoding='utf-8'))
    except Exception:
        c = dict(CFG_DEF)
    c['lugares'] = d
    json.dump(c, open(CFG, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)


def lugar_actualizar_def(a, claves):
    """Aplica --quitar o añade a la definición guardada del lugar. claves: {opción: campo del JSON}. Devuelve (nombre, definición)."""
    todos = lugares_cargar()
    nombre = a.nombre
    k = next((x for x in todos if sin_acentos(x).lower() == sin_acentos(nombre).lower()), nombre)
    d = todos.get(k) or {}
    existia = k in todos
    cambio = False
    for opt, campo in claves.items():
        vals = getattr(a, opt, None) or []
        lista = list(d.get(campo, []))
        for v in vals:
            if a.quitar:
                if v in lista:
                    lista.remove(v)
                    cambio = True
            elif v not in lista:
                lista.append(v)
                cambio = True
        d[campo] = lista
    if a.quitar and not any(getattr(a, o, None) for o in claves):
        todos.pop(k, None)
        lugares_guardar(todos)
        print('Lugar "%s" borrado.' % k)
        return k, None
    todos[k] = d
    if cambio or not existia:
        lugares_guardar(todos)
    return k, d


def cmd_lugares(a):
    todos = lugares_cargar()
    if not todos:
        print('No hay lugares guardados. Crea uno, por ejemplo:')
        base = os.path.basename(DIR.rstrip('/'))
        if base.startswith('x_'):
            print('   python x.py lugar "La Adrada" --busca "La Adrada" --cuenta @ayto_laadrada')
        elif base.startswith('ig_'):
            print('   python ig.py lugar "La Adrada" --etiqueta laadrada --cuenta ayuntamientodelaadrada')
        else:
            print('   python fb.py lugar "La Adrada" --pagina "https://www.facebook.com/pages/La%20Adrada,%20%C3%81vila/110333458996497" --busca "La Adrada"')
        return
    for k, d in todos.items():
        print('%s' % k)
        for campo, vals in d.items():
            if vals:
                print('   %-10s %s' % (campo + ':', '  |  '.join(vals)))
    print('\nActualizar uno:  python fb.py lugar NOMBRE        Borrar uno:  python fb.py lugar NOMBRE --quitar')


def cmd_lugar(a):
    """Seguir un pueblo/ciudad: lee sus páginas y grupos, busca su nombre en publicaciones y enseña lo último."""
    k, d = lugar_actualizar_def(a, {'pagina': 'paginas', 'grupo': 'grupos', 'busca': 'busquedas'})
    if d is None:
        return
    paginas = list(d.get('paginas', [])) + list(d.get('grupos', []))
    busq = list(d.get('busquedas', []))
    if not paginas and not busq:
        sys.exit('El lugar "%s" no tiene nada que seguir. Añade --pagina URL, --grupo URL o --busca "texto".' % k)
    print('LUGAR: %s   (%d páginas/grupos, %d búsquedas)' % (k, len(paginas), len(busq)))
    if a.solo_guardar:
        print('Guardado (no se lee nada ahora). Para leerlo: python fb.py lugar "%s"' % k)
        return
    con = db()
    n = a.scroll if a.scroll is not None else int(config()['scrolls'])
    urls = [expandir_url(u) for u in paginas]
    if a.vigilar:
        for u in urls:
            registrar_vigilada(con, u)
        con.commit()
        print('Añadidas a VIGILADOS: ahora "lanzar" también las abre.')
    con.close()
    items = [(u, 'capturar', None, n, u[:60]) for u in urls] if not a.sin_paginas else []
    if items:
        asegurar_servidor()
        print('Leyendo %d páginas/grupos (%d publicaciones de cada una)...' % (len(items), n))
        lanzar_items(items, paquete_navegador(a), a.espera, a.pausa, volver=True)
    if busq and not a.sin_buscar:
        for t in busq:
            print('\nBuscando publicaciones con "%s"...' % t)
            ns = argparse.Namespace(consulta=[t], exacta=False, tipo=a.tipo, elegir=None, no_preguntar=True, sin_abrir=False,
                                    scroll=None, n=5, no_marcar=True, espera=a.espera, pausa=a.pausa, navegador=a.navegador,
                                    enlaces=None, reacciones=False)
            try:
                cmd_fbuscar(ns)
            except SystemExit as e:
                print('   (no terminó: %s)' % e)
    con = db()
    fids = []
    for u in urls:
        ff = fuente_por_clave(con, clave_de_url(u))
        if ff:
            fids.append(ff['id'])
    if fids:
        print('\n' + '=' * ancho())
        print('LO ÚLTIMO DE %s (más reciente primero)' % k.upper())
        print('=' * ancho() + '\n')
        todos = []
        for p in con.execute('SELECT p.*, f.nombre AS fnombre FROM publicaciones p JOIN fuentes f ON f.id=p.fuente_id WHERE p.fuente_id IN (%s)' % ','.join('?' * len(fids)), fids).fetchall():
            todos.append(((p['fecha_aprox'] or p['primera_vez'] or '')[:16], p['id'], p))
        todos.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for _, _, p in todos[:a.mostrar]:
            mostrar_post(con, p, marcar=not a.no_marcar, fnombre=p['fnombre'])
        con.commit()
        if not todos:
            print('0 publicaciones leídas de esas páginas. Si la página existe y tiene contenido: python fb.py diagnostico')
        print('\nUn día concreto de este lugar:  python fb.py dia %d --dia ayer' % fids[0])




# ----------------------------------------------------------------------------
# actividad propia (Registro de actividad de Facebook) y lo recibido
# ----------------------------------------------------------------------------

CATEGORIAS_ACT = {   # nombre corto -> category_key del Registro de actividad
    'reacciones': 'YOURACTIVITYCOMMENTSANDREACTIONSSCHEMA',
    'comentarios': 'COMMENTSCLUSTER',
    'publicaciones': 'YOURACTIVITYPOSTSSCHEMA',
    'grupos-publicaciones': 'GROUPPOSTS',
    'grupos': 'YOURACTIVITYGROUPSSCHEMA',
    'grupos-reacciones': 'GROUPREACTIONS',
    'grupos-miembro': 'GROUPMEMBERSHIP',
    'contribuciones': 'COMMUNITYCONTRIBUTIONS',
    'historias': 'STORIESFEEDBACK',
    'intereses': 'LIKEDINTERESTS',
    'busquedas': 'SEARCH',
    'videos-vistos': 'VIDEOWATCH',
    'paginas': 'YOURACTIVITYPAGESSCHEMA',
    'megusta': 'LIKEDPOSTS',
    'etiquetas': 'YOURACTIVITYTAGGEDINSCHEMA',
}
CATEGORIAS_POR_DEFECTO = ['reacciones', 'megusta', 'publicaciones', 'grupos-publicaciones', 'grupos-reacciones', 'paginas', 'etiquetas']
TIPOS_ACT = ('publicacion', 'comentario', 'me gusta', 'reaccion', 'mencion', 'grupo', 'pagina', 'otro')


def tipo_actividad(titulo):
    t = sin_acentos((titulo or '').lower())
    if re.search(r'\b(le|te) gust(a|o)\b', t):
        return 'me gusta'
    if 'reacciono' in t:
        return 'reaccion'
    if re.search(r'\b(comento|respondio|ha comentado)\b', t):
        return 'comentario'
    if 'mencion' in t or 'etiquet' in t:
        return 'mencion'
    if re.search(r'miembro|te uniste|se unio|solicitud', t):
        return 'grupo'
    if re.search(r'\b(publico|agrego|actualizo|compartio|escribio|subio|cambio su foto|creo)\b', t):
        return 'publicacion'
    return 'otro'


def fecha_actividad(txt):
    """'12 de octubre de 2026' -> '2026-10-12'; 'Hoy'/'Ayer' también; si no se entiende, ''."""
    r = parse_tiempo(txt or '', datetime.now())
    return r[:10] if r else ''


def hora_24(h):
    m = re.match(r'^(\d{1,2}):(\d{2})\s*([ap])?', (h or '').strip().lower().replace('.', ''))
    if not m:
        return ''
    hh = int(m.group(1))
    if m.group(3) == 'p' and hh < 12:
        hh += 12
    if m.group(3) == 'a' and hh == 12:
        hh = 0
    return '%02d:%s' % (hh, m.group(2))


def perfil_nombre(con, pid):
    r = con.execute('SELECT nombre FROM perfiles WHERE perfil=?', (str(pid),)).fetchone()
    return (r['nombre'] if r and r['nombre'] else str(pid))


def procesar_actividad(con, d):
    t0 = ahora()
    perfil = str(d.get('perfil') or '')
    cat = d.get('categoria') or 'ALL'
    items = d.get('actividad') or []
    pid_re = re.compile(r'(?:[?&]id=|facebook\.com/)' + re.escape(perfil) + r'(?:\b|/|$)') if perfil else None
    con.execute('INSERT OR IGNORE INTO perfiles(perfil, nombre, creado) VALUES(?,?,?)', (perfil, '', t0))
    nuevos = 0
    for it in items:
        titulo = (it.get('titulo') or '').strip()
        if not titulo:
            continue
        enl = it.get('enlaces') or []
        if pid_re:   # el nombre del perfil se aprende del enlace propio que aparece en el título
            for e in enl:
                if pid_re.search(e.get('href') or '') and e.get('texto'):
                    con.execute("UPDATE perfiles SET nombre=? WHERE perfil=? AND COALESCE(nombre,'')=''", (e['texto'], perfil))
                    break
        objetivo = ''
        for e in reversed(enl):
            if e.get('texto') and not (pid_re and pid_re.search(e.get('href') or '')):
                objetivo = e['texto']
                break
        fecha = fecha_actividad(it.get('fecha'))
        hora = hora_24(it.get('hora'))
        tipo = tipo_actividad(titulo)
        f = con.execute('SELECT id FROM actividad WHERE perfil=? AND titulo=? AND fecha=? AND hora=?', (perfil, titulo, fecha, hora)).fetchone()
        if f:
            con.execute('UPDATE actividad SET ultima_vez=?, categoria=CASE WHEN categoria LIKE ? THEN categoria ELSE categoria||?||? END WHERE id=?',
                        (t0, '%' + cat + '%', ',', cat, f['id']))
            continue
        con.execute('INSERT INTO actividad(perfil,categoria,titulo,tipo,objetivo,fecha,hora,publico,texto,url,enlaces,primera_vez,ultima_vez) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (perfil, cat, titulo, tipo, objetivo, fecha, hora, it.get('publico') or '', it.get('texto') or '', it.get('ver') or '',
                     json.dumps(enl, ensure_ascii=False), t0, t0))
        nuevos += 1
    nombre = perfil_nombre(con, perfil)
    res = {'ok': True, 'fuente_id': None, 'fuente': 'actividad de ' + nombre, 'posts': len(items), 'nuevos': nuevos,
           'resumen': 'Actividad de %s [%s]: %d elementos leídos, %d nuevos' % (nombre, cat, len(items), nuevos)}
    return res, None



def tipo_aviso(texto, extracto=''):
    """Clasifica un aviso (X/Instagram) por su frase, en español, inglés y alemán; lo que no se reconoce queda como 'otro'."""
    t = sin_acentos((texto or '').lower())
    if re.search(r'folgt dir|te sigue|te ha empezado|started following|followed you|follows you|ha comenzado a seguirte|empezo a seguirte|abonnement', t):
        return 'seguidor'
    if re.search(r'gefallt|le gusta|les gusta|liked|likes your|ha gustado|le ha gustado', t):
        return 'me gusta'
    if re.search(r'repostet|ha republicado|reposted|retweeted|republico|ha compartido|reposteo', t):
        return 'repost'
    if re.search(r'zitiert|ha citado|quoted|cito tu', t):
        return 'cita'
    if re.search(r'antwortet|respondio|ha respondido|replied|ha comentado|commented|comento|kommentiert', t):
        return 'respuesta'
    if re.search(r'erwahnt|te ha mencionado|te menciono|mentioned you|mencion', t):
        return 'mencion'
    return 'otro'


def procesar_avisos(con, d):
    red = 'x' if d.get('tipo') == 'x_avisos' else 'ig'
    yo = (d.get('yo') or '').lstrip('@')
    t0 = ahora()
    nuevos = 0
    items = d.get('avisos') or []
    for v in items:
        q = v.get('quien') or []
        quien = ', '.join(('%s @%s' % (x.get('nombre'), x.get('handle'))).strip() if x.get('nombre') else '@' + x.get('handle', '') for x in q)
        quien_url = (q[0].get('url') or ('https://x.com/' + q[0]['handle'] if red == 'x' and q else '')) if q else ''
        iso = v.get('fecha_iso') or ''
        fecha, hora = ('', '')
        if iso:
            try:
                dt = datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone()
                fecha, hora = dt.strftime('%Y-%m-%d'), dt.strftime('%H:%M')
            except Exception:
                fecha = iso[:10]
        elif v.get('fecha_texto'):
            fecha = (parse_tiempo(v['fecha_texto'], datetime.now()) or '')[:10]
        tipo = tipo_aviso(v.get('texto'), v.get('extracto'))
        clave_post = v.get('post_url') or ''
        ya = con.execute('SELECT id FROM avisos WHERE red=? AND cuenta=? AND texto=? AND post_url=? AND fecha=? AND hora=?',
                         (red, yo, v.get('texto') or '', clave_post, fecha, hora)).fetchone()
        if ya:
            con.execute('UPDATE avisos SET ultima_vez=? WHERE id=?', (t0, ya['id']))
            continue
        con.execute('INSERT INTO avisos(red,cuenta,tipo,quien,quien_url,texto,extracto,post_url,fecha,hora,primera_vez,ultima_vez) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (red, yo, tipo, quien, quien_url, v.get('texto') or '', v.get('extracto') or '', clave_post, fecha, hora, t0, t0))
        nuevos += 1
    res = {'ok': True, 'fuente_id': None, 'fuente': 'avisos de ' + red.upper(), 'posts': len(items), 'nuevos': nuevos,
           'resumen': 'Avisos de %s: %d leídos, %d nuevos' % (red.upper(), len(items), nuevos)}
    return res, None


def mostrar_avisos(con, red, a, titulo):
    q, par = 'SELECT * FROM avisos WHERE red=?', [red]
    if getattr(a, 'tipo', None):
        ts = [sin_acentos(x.lower()) for x in re.split(r'[,\s]+', a.tipo) if x]
        ts = [{'megusta': 'me gusta', 'seguidores': 'seguidor', 'respuestas': 'respuesta', 'menciones': 'mencion', 'reposts': 'repost', 'citas': 'cita'}.get(x, x) for x in ts]
        q += ' AND tipo IN (%s)' % ','.join('?' * len(ts))
        par += ts
    if any(getattr(a, k, None) for k in ('dia', 'desde', 'hasta', 'ultimos_dias')):
        rs = rangos_de(a)
        q += ' AND (' + ' OR '.join(['(fecha>=? AND fecha<=?)'] * len(rs)) + ')'
        for d1, d2 in rs:
            par += [str(d1), str(d2)]
    q += ' ORDER BY fecha DESC, hora DESC, id DESC'
    filas = con.execute(q, par).fetchall()
    if not filas:
        print('No hay avisos guardados con esos criterios.')
        return
    tot = len(filas)
    if a.n:
        filas = filas[:a.n]
    print(tabla(['Día', 'Hora', 'Tipo', 'Quién', 'Qué'],
                [[r['fecha'] or '?', r['hora'] or '', r['tipo'], r['quien'][:40], (r['texto'] + ((' — "' + r['extracto'][:80] + '"') if r['extracto'] else '') + ((' ' + r['post_url']) if a.urls and r['post_url'] else ''))] for r in filas]))
    print('\n%d avisos%s. %s' % (len(filas), (' de %d (-n 0 para todos)' % tot) if len(filas) < tot else '', titulo))


def resolver_perfil(con, txt):
    """id numérico, Nº de 'perfiles' o parte del nombre -> id de perfil, o None."""
    filas = con.execute('SELECT * FROM perfiles ORDER BY creado, perfil').fetchall()
    t = (txt or '').strip()
    if not t:
        return None
    if re.fullmatch(r'\d{1,3}', t) and int(t) <= len(filas):
        return filas[int(t) - 1]['perfil']
    ts = sin_acentos(t.lower())
    for r in filas:
        if ts == r['perfil'].lower():
            return r['perfil']
    for r in filas:
        if ts in sin_acentos((r['nombre'] or '').lower()):
            return r['perfil']
    if re.fullmatch(r'\d{9,}', t):
        return t
    return None


def cmd_perfiles(a):
    """Perfiles propios (personal y páginas que administras) de los que se lee el Registro de actividad."""
    con = db()
    if a.anadir:
        pid = a.anadir
        m = re.match(r'^(?:https?://(?:www\.)?facebook\.com/)?(?:profile\.php\?id=)?([A-Za-z0-9.\-]+)', pid)
        pid = m.group(1) if m else pid
        if not re.fullmatch(r'[A-Za-z0-9.\-]{5,}', pid):
            sys.exit('Indica el perfil por su número (profile.php?id=NÚMERO) o por el nombre de su dirección (facebook.com/NOMBRE), o pega la URL.')
        con.execute('INSERT OR IGNORE INTO perfiles(perfil, nombre, creado) VALUES(?,?,?)', (pid, '', ahora()))
        if a.nombre:
            con.execute('UPDATE perfiles SET nombre=? WHERE perfil=?', (' '.join(a.nombre), pid))
        con.commit()
        print('Perfil %s guardado.' % pid)
    if a.quitar:
        pid = resolver_perfil(con, a.quitar)
        if not pid:
            sys.exit('No encuentro ese perfil.')
        con.execute('DELETE FROM perfiles WHERE perfil=?', (pid,))
        con.commit()
        print('Perfil %s quitado de la lista (su actividad guardada se conserva).' % pid)
    filas = con.execute('SELECT * FROM perfiles ORDER BY creado, perfil').fetchall()
    if not filas:
        print('Todavía no hay perfiles. Añade el tuyo:  python fb.py perfiles --anadir NÚMERO [nombre]')
        print('(el número es el de tu perfil: facebook.com/profile.php?id=NÚMERO; también sale en la dirección de tu Registro de actividad)')
        return
    out = []
    for i, r in enumerate(filas, 1):
        n = con.execute('SELECT COUNT(*) FROM actividad WHERE perfil=?', (r['perfil'],)).fetchone()[0]
        u = con.execute('SELECT MAX(ultima_vez) FROM actividad WHERE perfil=?', (r['perfil'],)).fetchone()[0]
        out.append([i, r['perfil'], r['nombre'] or '(sin nombre)', n, (u or 'nunca')[:16]])
    print(tabla_c(['Nº', 'Perfil', 'Nombre', 'Elementos', 'Última lectura'], out))
    print('\nLeer su actividad:  python fb.py actividad Nº --leer     Verla:  python fb.py actividad Nº')


def urls_actividad(pid, cats):
    return ['https://www.facebook.com/%s/allactivity?activity_history=false&category_key=%s&manage_mode=false&should_load_landing_page=false'
            % (pid, CATEGORIAS_ACT.get(c, c)) for c in cats]


def lista_categorias(txt):
    if not txt:
        return list(CATEGORIAS_POR_DEFECTO)
    if txt.strip().lower() == 'todas':
        return list(CATEGORIAS_ACT)
    out = []
    for c in re.split(r'[,\s]+', txt.strip().lower()):
        c = sin_acentos(c)
        c = {'reaccion': 'reacciones', 'comentario': 'comentarios', 'publicacion': 'publicaciones',
             'pagina': 'paginas', 'etiqueta': 'etiquetas', 'historia': 'historias', 'busqueda': 'busquedas', 'interes': 'intereses', 'contribucion': 'contribuciones', 'menciones': 'etiquetas', 'gposts': 'grupos-publicaciones'}.get(c, c)
        if c not in CATEGORIAS_ACT:
            if c.isupper() or c.startswith('yourac'):
                out.append(c.upper())
                continue
            sys.exit('Categoría desconocida: "%s". Usa: %s, todas' % (c, ', '.join(CATEGORIAS_ACT)))
        out.append(c)
    return out


def filtrar_actividad(con, a, pid):
    q, par = 'SELECT * FROM actividad WHERE 1=1', []
    if pid:
        q += ' AND perfil=?'
        par.append(pid)
    if getattr(a, 'tipo', None):
        ts = [sin_acentos(x.lower()) for x in re.split(r'[,\s]+', a.tipo) if x]
        ts = [{'megusta': 'me gusta', 'reacciones': 'reaccion', 'comentarios': 'comentario', 'publicaciones': 'publicacion',
               'menciones': 'mencion', 'grupos': 'grupo', 'paginas': 'pagina'}.get(x, x) for x in ts]
        q += ' AND tipo IN (%s)' % ','.join('?' * len(ts))
        par += ts
    if getattr(a, 'texto', None):
        q += ' AND (titulo LIKE ? OR texto LIKE ? OR objetivo LIKE ?)'
        par += ['%' + a.texto + '%'] * 3
    if any(getattr(a, k, None) for k in ('dia', 'desde', 'hasta', 'ultimos_dias')):
        rs = rangos_de(a)
        q += ' AND (' + ' OR '.join(['(fecha>=? AND fecha<=?)'] * len(rs)) + ')'
        for d1, d2 in rs:
            par += [str(d1), str(d2)]
    q += ' ORDER BY fecha DESC, hora DESC, id DESC'
    return con.execute(q, par).fetchall()


def leer_actividad(a, con, pid, cats):
    urls = urls_actividad(pid, cats)
    sc = a.scroll if a.scroll is not None else 6
    items = [(u, 'capturar', None, sc, 'actividad %s [%s]' % (perfil_nombre(con, pid), c)) for u, c in zip(urls, cats)]
    con.close()
    asegurar_servidor()
    print('Leyendo el Registro de actividad del perfil %s (%d apartados, %d pantallas cada uno).' % (pid, len(items), sc))
    print('IMPORTANTE: en Facebook debes estar actuando COMO ese perfil (cambia de perfil antes si es una página).')
    res = lanzar_items(items, paquete_navegador(a), a.espera, a.pausa, volver=True)
    return [r for r in res if r[1] != 'hecha']


def cmd_actividad(a):
    """Tu actividad (publicaciones, comentarios, me gusta, reacciones, menciones...) por perfil, leída del Registro de actividad."""
    con = db()
    pid = resolver_perfil(con, a.perfil) if a.perfil else None
    if a.perfil and not pid:
        sys.exit('No conozco el perfil "%s". Mira  python fb.py perfiles  o añádelo con  perfiles --anadir NÚMERO nombre.' % a.perfil)
    if a.leer:
        ids = [pid] if pid else [r['perfil'] for r in con.execute('SELECT perfil FROM perfiles ORDER BY creado').fetchall()]
        if not ids:
            sys.exit('Indica el perfil (número) o añádelo antes:  python fb.py perfiles --anadir NÚMERO nombre')
        cats = lista_categorias(a.categoria)
        fallos = []
        for i in ids:
            con = db()
            fallos += leer_actividad(a, con, i, cats)
        con = db()
        if fallos:
            print('\nNo se pudieron leer %d apartados (mira  python fb.py cola ).' % len(fallos))
    filas = filtrar_actividad(con, a, pid)
    if not filas:
        print('No hay actividad guardada con esos criterios. Léela con:  python fb.py actividad PERFIL --leer')
        return
    if a.resumen:
        cuenta = {}
        for r in filas:
            cuenta.setdefault((r['perfil'], r['fecha']), {}).setdefault(r['tipo'], 0)
            cuenta[(r['perfil'], r['fecha'])][r['tipo']] += 1
        out = []
        for (p, f), d in sorted(cuenta.items(), key=lambda x: x[0][1], reverse=True)[: (a.n or 10000)]:
            out.append([f or '?', perfil_nombre(con, p)] + [d.get(t, 0) or '' for t in ('publicacion', 'comentario', 'me gusta', 'reaccion', 'mencion', 'grupo', 'otro')])
        print(tabla(['Día', 'Perfil', 'Publ.', 'Coment.', 'Me gusta', 'Reacc.', 'Menc.', 'Grupo', 'Otro'], out))
        return
    total = len(filas)
    if a.n:
        filas = filas[:a.n]
    ult = None
    for r in filas:
        if (r['fecha'], r['perfil']) != ult:
            ult = (r['fecha'], r['perfil'])
            print('\n== %s · %s ==' % (r['fecha'] or 'sin fecha', perfil_nombre(con, r['perfil'])))
        print('%s  [%s] %s' % (r['hora'] or '--:--', r['tipo'], r['titulo']))
        if r['texto']:
            print('       "%s"' % (r['texto'] if a.completo or len(r['texto']) <= 200 else r['texto'][:200] + '…'))
        if r['publico']:
            print('       (%s)' % r['publico'])
        if a.urls and r['url']:
            print('       ' + r['url'])
    print('\n%d elementos%s.' % (len(filas), (' de %d (usa -n 0 para todos)' % total) if len(filas) < total else ''))


def token_post(url):
    for pat in (r'(pfbid\w+)', r'story_fbid=(\w+)', r'/posts/(\w+)', r'/permalink/(\d+)', r'/videos/(\d+)', r'fbid=(\d+)'):
        m = re.search(pat, url or '')
        if m:
            return m.group(1)
    return ''


def publicacion_de_url(con, url):
    t = token_post(url)
    if not t:
        return None
    return con.execute("SELECT * FROM publicaciones WHERE permalink LIKE ? OR clave LIKE ? ORDER BY ncomentarios DESC, id DESC LIMIT 1",
                       ('%' + t + '%', '%' + t + '%')).fetchone()


def cmd_recibido(a):
    """Reacciones y comentarios RECIBIDOS: en tus publicaciones (por defecto) o en las publicaciones ajenas donde comentaste (--en-otras)."""
    con = db()
    pid = resolver_perfil(con, a.perfil) if a.perfil else None
    if a.perfil and not pid:
        sys.exit('No conozco el perfil "%s". Mira  python fb.py perfiles' % a.perfil)
    a.tipo = 'comentario' if a.en_otras else 'publicacion'
    a.texto = None
    filas = [r for r in filtrar_actividad(con, a, pid) if r['url']]
    if not filas:
        sys.exit('No hay actividad guardada de ese tipo con enlace. Primero:  python fb.py actividad PERFIL --leer')
    vistos, lista = set(), []
    for r in filas:
        k = token_post(r['url']) or r['url']
        if k not in vistos:
            vistos.add(k)
            lista.append(r)
    if a.n:
        lista = lista[:a.n]
    if a.leer:
        items = [(r['url'], 'capturar', None, 0, (r['titulo'] or '')[:50]) for r in lista]
        con.close()
        asegurar_servidor()
        print('Abriendo %d publicaciones para leer sus comentarios (%s).' % (len(items), 'donde tú comentaste' if a.en_otras else 'tuyas'))
        lanzar_items(items, paquete_navegador(a), a.espera, a.pausa, volver=False)
        if a.detalle and not a.en_otras:
            con = db()
            det = []
            for r in lista:
                p = publicacion_de_url(con, r['url'])
                if p and p['permalink']:
                    det.append((url_post_de_foto(con, p), 'detalle', p['id'], 0, '#%d' % p['id']))
            con.close()
            if det:
                print('Pidiendo quién reaccionó en %d publicaciones.' % len(det))
                lanzar_items(det, paquete_navegador(a), a.espera, a.pausa, volver=False)
        volver_a_termux()
        con = db()
    sin = 0
    for r in lista:
        p = publicacion_de_url(con, r['url'])
        print('\n' + '=' * ancho())
        print('%s %s · %s' % (r['fecha'], r['hora'], r['titulo']))
        if r['texto']:
            print('  Lo tuyo: "%s"' % r['texto'][:200])
        print('  ' + r['url'])
        if not p:
            sin += 1
            print('  (aún no leída: usa --leer)')
            continue
        print('  Reacciones: %s   Comentarios: %s   Compartidos: %s   (leído %s)' % (
            p['reacciones'] if p['reacciones'] is not None else '?', p['ncomentarios'] if p['ncomentarios'] is not None else '?',
            p['ncompartidos'] if p['ncompartidos'] is not None else '?', (p['ultima_vez'] or '')[:16]))
        if a.reacciones:
            for x in con.execute('SELECT nombre, reaccion, url FROM reaccionantes WHERE pub_id=? ORDER BY id', (p['id'],)).fetchall():
                print('    · %s %s%s' % (x['nombre'], '(' + x['reaccion'] + ')' if x['reaccion'] else '', ' ' + x['url'] if a.enlaces and x['url'] else ''))
            if not con.execute('SELECT 1 FROM reaccionantes WHERE pub_id=?', (p['id'],)).fetchone():
                print('    (sin lista de quién reaccionó: usa --leer --detalle, o  python fb.py detalle %d)' % p['id'])
        nombre_yo = sin_acentos(perfil_nombre(con, r['perfil']).lower())
        for c in con.execute('SELECT * FROM comentarios WHERE pub_id=? ORDER BY id', (p['id'],)).fetchall():
            mio = nombre_yo and nombre_yo in sin_acentos((c['autor'] or '').lower())
            if a.en_otras and not a.todos and not mio and not c['respuesta']:
                continue
            print('    %s %s: %s' % ('>>' if mio else '  ', c['autor'], (c['texto'] or '').replace('\n', ' ')[:300]))
    if sin:
        print('\n%d sin leer todavía.' % sin)
    con.commit()


def cmd_diagnostico(a):
    d = ruta('diagnosticos')
    fs = sorted(os.listdir(d)) if os.path.isdir(d) else []
    if not fs:
        print('No hay informes. Se crean solos cuando una captura sale con 0 publicaciones, o con el botón')
        print('"Diagnóstico de esta página" del panel de la extensión.')
        return
    dc = dir_diag_compartida()
    if dc:
        print('Copia accesible desde Android (Archivos > Descargas): %s' % dc)
    print('Informes en %s (%d). Último:\n' % (d, len(fs)))
    print(open(os.path.join(d, fs[-1]), encoding='utf-8').read())


def cmd_post(a):
    con = db()
    p = con.execute('SELECT * FROM publicaciones WHERE id=?', (a.id,)).fetchone()
    if not p:
        sys.exit('No existe la publicación %s' % a.id)
    mostrar_post(con, p, marcar=True)
    con.commit()


TIPOS_LISTA = {'contactos': 'contacto', 'contacto': 'contacto', 'paginas': 'pagina', 'páginas': 'pagina',
               'pagina': 'pagina', 'grupos': 'grupo', 'grupo': 'grupo', 'amigos': 'amigo', 'amigo': 'amigo',
               'personas': 'siguiendo', 'siguiendo': 'siguiendo', 'miembros': 'miembro', 'miembro': 'miembro',
               'conversaciones': 'conversacion', 'conversacion': 'conversacion'}
ROTULO = {'contacto': 'Contactos (columna de Messenger)', 'pagina': 'Páginas', 'grupo': 'Grupos', 'amigo': 'Amigos',
          'siguiendo': 'Personas/páginas que sigo', 'miembro': 'Miembros de grupos', 'conversacion': 'Conversaciones'}


def cmd_listas(a):
    con = db()
    if not a.tipo:
        filas = con.execute('SELECT tipo, COUNT(*) n, SUM(visto=0) nuevos FROM listas WHERE baja IS NULL GROUP BY tipo').fetchall()
        if not filas:
            print('Todavía no hay listas. Captura la página principal (contactos, grupos y accesos directos),')
            print('facebook.com/friends/list, facebook.com/pages/?category=liked, etc.')
            return
        print(tabla(['Lista', 'Elementos', 'Nuevos'], [[ROTULO.get(r['tipo'], r['tipo']) + ' [' + r['tipo'] + 's]', r['n'], r['nuevos'] or 0] for r in filas]))
        print('\nVer una lista:  python fb.py listas contactos | paginas | grupos | amigos | personas | miembros')
        return
    t = TIPOS_LISTA.get(a.tipo.lower())
    if not t:
        sys.exit('Tipo desconocido. Usa: contactos, paginas, grupos, amigos, personas, miembros, conversaciones')
    q = 'SELECT * FROM listas WHERE tipo=?' + (' AND baja IS NOT NULL' if getattr(a, 'bajas', False) else ' AND baja IS NULL')
    par = [t]
    if a.nuevos:
        q += ' AND visto=0'
    if a.ambito:
        q += ' AND ambito LIKE ?'
        par.append('%' + a.ambito + '%')
    q += ' ORDER BY (visto=0) DESC, primera_vez DESC, nombre'
    filas = con.execute(q, par).fetchall()
    if not filas:
        if t == 'contacto':
            print('No hay ningún contacto guardado: la columna de contactos de la página principal no se ha capturado o está vacía.')
            print('Captura la principal con:  python fb.py actualizar --solo inicio   (o: python fb.py lanzar --listas)')
        else:
            print('Lista vacía (nada guardado todavía en "%s").' % ROTULO.get(t, t))
        return
    print('cantidad: %d\n%s' % (len(filas), ROTULO.get(t, t)) + '\n')
    cab = ['Ref', '', 'Nombre', 'Public.', '1ª vez', 'URL']
    rows = []
    for r in filas:
        f = fuente_de_lista(con, r)
        npub = con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (f['id'],)).fetchone()[0] if f else '-'
        rows.append(['%s%d' % (TIPO_LETRA.get(r['tipo'], '?'), r['id']), '*' if r['visto'] == 0 else '', r['nombre'], npub, r['primera_vez'][:10], r['url']])
    print(tabla(cab, rows))
    print('\n(* = nuevo desde la última vez que miraste esta lista;  Public. = publicaciones ya capturadas)')
    ej = rows[0][0]
    print('Usa la referencia (columna Ref) en cualquier orden, por ejemplo:')
    print('   python fb.py vigilar %s          captura sus publicaciones' % ej)
    print('   python fb.py ultimas %s -n 5     lee las 5 últimas' % ej)
    if not a.no_marcar:
        con.execute('UPDATE listas SET visto=1 WHERE tipo=?' + (' AND ambito LIKE ?' if a.ambito else ''), par if a.ambito else [t])
        con.commit()


def cmd_mensajes(a):
    con = db()
    if a.conv and REF_RE.match(a.conv):
        l = lista_por_ref(con, a.conv)
        m = re.search(r'/messages/t/([^/]+)', l['url'] or '')
        a.conv = m.group(1) if m else l['nombre']
    q = 'SELECT * FROM mensajes'
    par = []
    w = []
    if a.nuevos:
        w.append('visto=0')
    if a.conv:
        w.append('(conv=? OR lower(conv_nombre) LIKE ?)')
        par += [a.conv, '%' + a.conv.lower() + '%']
    if w:
        q += ' WHERE ' + ' AND '.join(w)
    q += ' ORDER BY conv, id'
    filas = con.execute(q, par).fetchall()
    if not filas:
        print('No hay mensajes guardados. Abre una conversación en facebook.com/messages/t/... y captúrala con la extensión.')
        cs = con.execute("SELECT * FROM listas WHERE tipo='conversacion' ORDER BY primera_vez DESC").fetchall()
        if cs:
            print('\nConversaciones vistas:')
            print(tabla(['Nombre', 'Último mensaje visible'], [[c['nombre'], c['extra']] for c in cs]))
        return
    ult = None
    for m in filas:
        if m['conv'] != ult:
            print('\n=== Conversación: %s ===' % (m['conv_nombre'] or m['conv']))
            ult = m['conv']
        print(('[NUEVO] ' if m['visto'] == 0 else '') + envolver(m['texto'], '  ').lstrip())
    con.execute('UPDATE mensajes SET visto=1')
    con.commit()


def renombrar_imagenes(con, fid=None):
    """Pone a las imágenes ya descargadas el nombre nuevo (fecha de la publicación + fuente)."""
    q = """SELECT i.id, i.archivo, p.clave AS pclave, p.primera_vez, p.fecha_aprox, f.clave AS fclave, f.nombre AS fnombre,
                  (SELECT COUNT(*) FROM imagenes j WHERE j.pub_id=i.pub_id AND j.id<=i.id) AS k
           FROM imagenes i JOIN publicaciones p ON p.id=i.pub_id JOIN fuentes f ON f.id=p.fuente_id
           WHERE i.estado='ok' AND COALESCE(i.archivo,'')<>'' AND i.url NOT LIKE 'fotograma:%'"""
    par = ()
    if fid:
        q += ' AND f.id=?'
        par = (fid,)
    n = 0
    for r in con.execute(q, par).fetchall():
        if not os.path.exists(r['archivo']):
            continue
        ext = os.path.splitext(r['archivo'])[1]
        nuevo = os.path.join(os.path.dirname(r['archivo']), nombre_imagen(r['fecha_aprox'], r['primera_vez'], r['fnombre'], r['fclave'], r['pclave'], r['k']) + ext)
        if nuevo == r['archivo'] or os.path.exists(nuevo):
            continue
        try:
            os.rename(r['archivo'], nuevo)
            con.execute('UPDATE imagenes SET archivo=? WHERE id=?', (nuevo, r['id']))
            n += 1
        except OSError:
            pass
    con.commit()
    return n


def cmd_repetidas(a):
    """Une las publicaciones repetidas ya guardadas (misma página/grupo, mismo autor, mismo día y texto igual o recortado)."""
    con = db()
    tot = 0
    for fu in con.execute("SELECT id, nombre FROM fuentes WHERE tipo NOT LIKE 'x\\_%' ESCAPE '\\' AND tipo NOT LIKE 'ig\\_%' ESCAPE '\\'").fetchall():
        ps = con.execute('SELECT id, autor, texto, fecha_aprox, permalink FROM publicaciones WHERE fuente_id=? ORDER BY id', (fu['id'],)).fetchall()
        borradas = set()
        for i, x in enumerate(ps):
            if x['id'] in borradas:
                continue
            for y in ps[i + 1:]:
                if y['id'] in borradas:
                    continue
                yd = {'texto': y['texto'], 'permalink': y['permalink']}
                fy, iy = huella_db(con, y)
                if not misma_pub(con, x, yd, (y['fecha_aprox'] or '')[:10], nombre_sin_seguir(y['autor']), norm_url(y['permalink']) if y['permalink'] else '', (fy, iy)):
                    continue
                tot += 1
                print('%s: #%d y #%d son la misma -> se queda #%d  (%s)' % (fu['nombre'], x['id'], y['id'], x['id'], (x['texto'] or '')[:50].replace('\n', ' ')))
                borradas.add(y['id'])
                if a.aplicar:
                    xt = con.execute('SELECT texto, permalink FROM publicaciones WHERE id=?', (x['id'],)).fetchone()
                    yt = con.execute('SELECT texto, permalink, detalle_vez, reacciones, ncomentarios, ncompartidos FROM publicaciones WHERE id=?', (y['id'],)).fetchone()
                    con.execute('UPDATE publicaciones SET texto=?, permalink=?, detalle_vez=COALESCE(detalle_vez,?), reacciones=MAX(COALESCE(reacciones,0),?), ncomentarios=MAX(COALESCE(ncomentarios,0),?), ncompartidos=MAX(COALESCE(ncompartidos,0),?) WHERE id=?',
                                (yt['texto'] if len(yt['texto'] or '') > len(xt['texto'] or '') else xt['texto'], mejor_permalink(xt['permalink'] or '', yt['permalink'] or ''),
                                 yt['detalle_vez'], yt['reacciones'] or 0, yt['ncomentarios'] or 0, yt['ncompartidos'] or 0, x['id']))
                    for t in ('comentarios', 'imagenes', 'enlaces', 'reaccionantes', 'compartidores'):
                        con.execute('UPDATE OR IGNORE %s SET pub_id=? WHERE pub_id=?' % t, (x['id'], y['id']))
                        con.execute('DELETE FROM %s WHERE pub_id=?' % t, (y['id'],))
                    con.execute('DELETE FROM publicaciones WHERE id=?', (y['id'],))
    if a.aplicar:
        con.commit()
        print('Unidas %d publicaciones repetidas.' % tot)
    else:
        print('%d repetidas encontradas. Para unirlas de verdad: python3 fb.py repetidas --aplicar' % tot)


# ----------------------------------------------------------------------------
# varias publicaciones por nombre, texto/descripción de imágenes, copiar, original compartida, fotogramas de vídeo
# ----------------------------------------------------------------------------

def resolver_pubs(con, tokens, n=5, solo_permalink=False):
    """Números de publicación (60 61) y/o páginas/grupos (P1, G3 o parte del nombre): de cada página, sus últimas n publicaciones."""
    ids = []
    for t in tokens or []:
        t = str(t).strip()
        if not t:
            continue
        if t.isdigit():
            ids.append(int(t))
            continue
        f = buscar_fuente(con, t)
        q = 'SELECT id FROM publicaciones WHERE fuente_id=? ' + ("AND permalink<>'' " if solo_permalink else '')
        q += 'ORDER BY COALESCE(fecha_aprox, primera_vez) DESC, id DESC LIMIT ?'
        ids += [r['id'] for r in con.execute(q, (f['id'], n)).fetchall()]
    return list(dict.fromkeys(ids))


def pubs_de_args(con, a, n_def=5):
    ids = resolver_pubs(con, getattr(a, 'ids', None) or [], getattr(a, 'n', None) or n_def)
    if not ids:
        sys.exit('Indica publicaciones: números (60 61) y/o páginas/grupos (P1 P2 G3) con -n cuántas de cada una.')
    return ids


def ocr_imagen(arch, psm=None, mejorar=False):
    """Texto de una imagen con tesseract (si está instalado). Devuelve (texto, mensaje_error).
    psm: modo de página de tesseract (11 = texto suelto repartido, 6 = bloque); mejorar: agranda x2 y sube el contraste antes de leer."""
    exe = shutil.which('tesseract')
    if not exe:
        return None, 'tesseract no está instalado (en Termux: pkg install tesseract)'
    try:
        r = subprocess.run([exe, '--list-langs'], capture_output=True, text=True, timeout=20)
        langs = {x.strip() for x in (r.stdout or '').splitlines()[1:]}
    except Exception:
        langs = set()
    lg = '+'.join(x for x in ('spa', 'eng') if x in langs) or ('eng' if 'eng' in langs else '')
    tmp_ = None
    if mejorar:
        try:
            from PIL import Image, ImageOps
            im_ = Image.open(arch).convert('L')
            im_ = im_.resize((im_.width * 2, im_.height * 2))
            im_ = ImageOps.autocontrast(im_)
            import tempfile
            tmp_ = tempfile.mktemp(suffix='.png', prefix='fbocr_')
            im_.save(tmp_)
        except Exception:
            tmp_ = None   # sin Pillow (pkg install python-pillow): se lee la imagen tal cual
    cmd = [exe, tmp_ or arch, 'stdout'] + (['-l', lg] if lg else []) + (['--psm', str(psm)] if psm else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, str(e)
    finally:
        if tmp_:
            try:
                os.remove(tmp_)
            except OSError:
                pass
    t = re.sub(r'\n{3,}', '\n\n', (r.stdout or '').strip())
    aviso = '' if 'spa' in langs else 'sin idioma español instalado (pkg search tesseract); se lee con lo disponible'
    return t, aviso


VISION_PROMPT = 'Describe this image in one or two short sentences: what is shown (people, place, objects).'
VISION_PROMPT_ES = 'Describe esta imagen en español en una o dos frases: qué se ve (personas, lugar, objetos, ambiente). No inventes nombres propios.'
VISION_MODELO = 'ggml-org/SmolVLM-256M-Instruct-GGUF:Q8_0'   # ~175 MB, código abierto (Apache-2.0), corre en el móvil con llama.cpp


def vision_cmd():
    """Orden para describir una imagen con un modelo local de código abierto. config.json "vision_cmd" (con {img} y {prompt}) o, si está
    llama.cpp (pkg install llama-cpp), SmolVLM-256M. None si no hay nada instalado."""
    c = (config().get('vision_cmd') or '').strip()
    if c:
        return c
    exe = shutil.which('llama-mtmd-cli')
    if exe:
        return '%s -hf %s --image {img} -p {prompt} -n 120 --temp 0.2' % (exe, VISION_MODELO)
    return None


def guardar_config(**kw):
    """Cambia claves de config.json conservando el resto."""
    c = {}
    if os.path.exists(CFG):
        try:
            c = json.load(open(CFG, encoding='utf-8'))
        except Exception:
            c = {}
    c.update(kw)
    json.dump(c, open(CFG, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)


def hay_vision():
    return bool((config().get('vision_ollama') or '').strip() or vision_cmd())


def _ollama_arrancado(url):
    try:
        urllib.request.urlopen(url.rstrip('/') + '/api/tags', timeout=3).read()
        return True
    except Exception:
        return False


def describir_con_ollama(arch, modelo, prompt):
    """Describe la imagen con un modelo de visión servido por Ollama (api local). Arranca 'ollama serve' si no está. (texto, error)"""
    url = (config().get('ollama_url') or 'http://127.0.0.1:11434').rstrip('/')
    if not _ollama_arrancado(url):
        exe = shutil.which('ollama')
        if not exe:
            return None, 'ollama no está instalado (en Termux: pkg install ollama)'
        try:
            subprocess.Popen([exe, 'serve'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as e:
            return None, 'no pude arrancar ollama: %s' % e
        for _ in range(30):
            time.sleep(1)
            if _ollama_arrancado(url):
                break
        else:
            return None, 'ollama no responde en %s (ejecuta  ollama serve  en otra sesión de Termux)' % url
    try:
        with open(arch, 'rb') as fh:
            b64 = base64.b64encode(fh.read()).decode()
        cuerpo = json.dumps({'model': modelo, 'prompt': prompt, 'images': [b64], 'stream': False,
                             'options': {'temperature': 0.2, 'num_predict': 220}}).encode()
        rq = urllib.request.Request(url + '/api/generate', data=cuerpo, headers={'Content-Type': 'application/json'})
        r = json.loads(urllib.request.urlopen(rq, timeout=int(config().get('vision_timeout') or 600)).read().decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        det = ''
        try:
            det = json.loads(e.read().decode('utf-8', 'replace')).get('error', '')
        except Exception:
            pass
        if e.code == 404 or 'not found' in det.lower():
            return None, 'el modelo %s no está descargado (ollama pull %s)' % (modelo, modelo)
        return None, 'ollama: %s' % (det or e)
    except Exception as e:
        return None, 'ollama: %s' % e
    return re.sub(r'\s+', ' ', r.get('response') or '').strip(), ''


def limpiar_ocr(t, maximo=350):
    """Del texto bruto de tesseract deja solo las líneas con palabras de verdad (en fotos saca mucha basura tipo «Il~ {;»)."""
    out = []
    for l in (t or '').splitlines():
        l = ' '.join(l.split())
        if len(l) < 4:
            continue
        pal = [w for w in re.findall(r'[^\W\d_]{3,}', l)]
        if not pal or sum(len(w) for w in pal) < 0.55 * len(l.replace(' ', '')):
            continue
        out.append(l)
    return ' / '.join(out)[:maximo]


def contexto_para_ia(ocr, pie, es_ingles=True):
    """Pista que se añade al prompt del modelo de visión: el texto leído por OCR y el pie de la publicación (la salida de una IA es la entrada de la otra)."""
    partes = []
    o = limpiar_ocr(ocr)
    if o:
        partes.append(('Text visible in the image (read by OCR, may contain errors): "%s".' if es_ingles else 'Texto visible en la imagen (leído por OCR, puede tener errores): «%s».') % o)
    pie = ' '.join((pie or '').split())[:200]
    if pie:
        partes.append(('Caption of the post: "%s".' if es_ingles else 'Pie de la publicación: «%s».') % pie)
    if partes:
        partes.append('Use this only as a hint; describe what is SEEN.' if es_ingles else 'Úsalo solo como pista; describe lo que SE VE.')
    return ' '.join(partes)


def describir_imagen_local(arch, contexto=''):
    """Qué se ve en la imagen, con un modelo local de código abierto (sin enviar nada a internet salvo la 1ª descarga del modelo). (descripcion, error)
    contexto: pista opcional (texto OCR y pie de la publicación) que se añade al prompt."""
    cfg = config()
    prompt = cfg.get('vision_prompt') or VISION_PROMPT
    mo = (cfg.get('vision_ollama') or '').strip()
    if mo:
        if not cfg.get('vision_prompt'):
            prompt = VISION_PROMPT_ES
        if contexto:
            prompt = prompt + ' ' + contexto
        return describir_con_ollama(arch, mo, prompt)
    if contexto:
        prompt = prompt + ' ' + contexto
    vc = vision_cmd()
    if not vc:
        return None, 'no hay modelo de visión local (mira: python3 fb.py ia)'
    import shlex
    try:
        cmd = [x.replace('{img}', arch).replace('{prompt}', prompt) for x in shlex.split(vc)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=int(cfg.get('vision_timeout') or 600))
    except subprocess.TimeoutExpired:
        return None, 'el modelo tardó demasiado (sube "vision_timeout" en config.json)'
    except Exception as e:
        return None, str(e)
    txt = (r.stdout or '').strip()
    txt = re.sub(r'\s+', ' ', re.sub(r'(?m)^(main|llama|clip|load|encoding|decoding|image|mtmd|ggml|build|sched)[^\n]*$', '', txt)).strip()
    if r.returncode != 0 and not txt:
        return None, ((r.stderr or '').strip().splitlines() or ['error %d' % r.returncode])[-1][:200]
    return txt, ''


def cmd_ia(a):
    """Estado de las herramientas locales (OCR y descripción de imágenes) y cómo instalarlas / activarlas."""
    if a.activar:
        guardar_config(describir_imagenes=True)
    if a.desactivar:
        guardar_config(describir_imagenes=False)
    if a.ollama is not None:
        guardar_config(vision_ollama=a.ollama.strip())
        print('Modelo de Ollama guardado: %s' % (a.ollama.strip() or '(ninguno: se usa llama.cpp/SmolVLM o vision_cmd)'))
    cfg = config()
    print('HERRAMIENTAS LOCALES (código abierto, sin cuentas ni claves)\n')
    print('Describir las imágenes automáticamente al terminar  lanzar : %s' % ('ACTIVADO' if cfg.get('describir_imagenes') else 'desactivado  (actívalo con: python3 fb.py ia --activar)'))
    print()
    t = shutil.which('tesseract')
    print('Texto de imágenes (OCR) - tesseract: %s' % ('instalado (%s)' % t if t else 'NO instalado  ->  pkg install tesseract'))
    if t:
        try:
            r = subprocess.run([t, '--list-langs'], capture_output=True, text=True, timeout=20)
            print('   idiomas: ' + ', '.join(x.strip() for x in (r.stdout or '').splitlines()[1:] if x.strip() and x.strip() != 'osd'))
            if 'spa' not in (r.stdout or ''):
                print('   falta el español: pkg search tesseract  (o descarga spa.traineddata en la carpeta tessdata)')
        except Exception:
            pass
    mo = (cfg.get('vision_ollama') or '').strip()
    vc = vision_cmd()
    print('\nQué se ve en la imagen (modelo de visión) : %s' % ('listo' if (mo or vc) else 'NO instalado'))
    if mo:
        print('   Ollama, modelo %s  (%s)   %s' % (mo, cfg.get('ollama_url'), 'ollama instalado' if shutil.which('ollama') else 'FALTA: pkg install ollama'))
        print('   (describe en español; tiene prioridad sobre llama.cpp)')
    elif vc:
        print('   orden: ' + vc)
    if not mo and not vc:
        print('   Opción mínima (SmolVLM-256M, ~175 MB, Apache-2.0, descripciones en inglés):')
        print('      pkg install llama-cpp')
        print('      (la primera vez que se use baja el modelo solo: %s)' % VISION_MODELO)
    print('   Opciones mejores (describen en español, tardan más y piden más memoria), con Ollama:')
    print('      pkg install ollama   y luego   ollama pull gemma3:4b   (o qwen2.5vl:3b, o moondream)')
    print('      python3 fb.py ia --ollama gemma3:4b')
    print('   Otra: pon en %s la clave "vision_cmd" con tu propia orden, usando {img} y {prompt}.' % ruta('config.json'))
    print('\nUn modelo de TRADUCCIÓN (alemán-inglés u otro) no sirve para describir imágenes: solo traduce texto.')
    if a.probar:
        print('\nPrueba con %s ...' % a.probar)
        t_, e_ = ocr_imagen(a.probar)
        print('OCR: %s' % (' '.join((t_ or '').split())[:300] or e_ or '(sin texto)'))
        d_, e_ = describir_imagen_local(a.probar)
        print('Se ve: %s' % (d_ or e_))


RE_DICE_TEXTO = re.compile(r'\b(text|texto|letters?|letras?|words?|palabras?|sign|signs|cartel|poster|banner|caption|title|titulo|título|writing|logo|label|inscription)\b', re.I)


def analizar_imagenes(ids, todas=False, rehacer=False, siempre_desc=False, limite=0, fotogramas=False, log=print):
    """OCR + descripción de las imágenes de esas publicaciones; lo guarda. Devuelve cuántas imágenes se han procesado."""
    con = db()
    hay_ia = hay_vision()
    hay_ocr = bool(shutil.which('tesseract'))
    if not hay_ocr and not hay_ia:
        log('No hay tesseract ni modelo de visión: no se puede leer ni describir nada. Mira: python3 fb.py ia')
        return 0
    if not hay_ocr or not hay_ia:
        log('Aviso: %s%s  (detalles: python3 fb.py ia)' % ('' if hay_ocr else 'falta tesseract (texto de imágenes). ', '' if hay_ia else 'falta el modelo de visión local (qué se ve; se usa mientras tanto la descripción de Facebook).'))
    hechas = 0
    for i in ids:
        if limite and hechas >= limite:
            log('Límite de %d imágenes alcanzado ("describir_max"); el resto queda para la próxima vez.' % limite)
            break
        p = con.execute('SELECT * FROM publicaciones WHERE id=?', (i,)).fetchone()
        if not p:
            log('#%s: no existe.' % i)
            continue
        filtro = '' if fotogramas else " AND url NOT LIKE 'fotograma:%'"
        ims = con.execute("SELECT * FROM imagenes WHERE pub_id=?" + filtro + " ORDER BY id", (i,)).fetchall()
        if not ims:
            log('#%d: no tiene imágenes.' % i)
            continue
        if len(ims) > 1 and not todas:
            log('#%d: tiene %d imágenes (solo se hacen las de 1 sola; usa --todas para todas).' % (i, len(ims)))
            continue
        for k, im in enumerate(ims, 1):
            if limite and hechas >= limite:
                break
            if not im['archivo'] or im['estado'] != 'ok' or not os.path.exists(im['archivo']):
                log('#%d img%d: no está descargada (%s).' % (i, k, im['estado']))
                continue
            hecha = im['ocr'] is not None and (im['descripcion'] or not (hay_ia or (im['alt'] or '')))
            if hecha and not rehacer:
                continue
            txt, aviso = (None, '')
            if hay_ocr:
                txt, aviso = ocr_imagen(im['archivo'])
            sin_texto = len(re.sub(r'\W+', '', txt or '')) < 15
            desc = ''
            if (sin_texto or siempre_desc) and hay_ia:
                # CADENA: 1) tesseract lee el texto  2) ese texto y el pie de la publicación pasan como pista al modelo que describe
                ctx = ''
                if config().get('vision_contexto', True):
                    ctx = contexto_para_ia(txt, ' '.join(x for x in (p['texto'], p['texto_compartido']) if x), es_ingles=not (config().get('vision_ollama') or '').strip() or bool(config().get('vision_prompt')))
                log('#%d img%d: describiendo la imagen con el modelo local%s (en el móvil puede tardar uno o dos minutos)...' % (i, k, ' (con el texto leído como pista)' if ctx else ''))
                d2, err = describir_imagen_local(im['archivo'], ctx)
                if err:
                    log('#%d img%d: modelo de visión: %s' % (i, k, err))
                else:
                    desc = d2 or ''
                # CADENA a la inversa: si la descripción dice que hay letras/cartel y tesseract no leyó nada, se reintenta con modo de texto suelto y la imagen mejorada
                if desc and hay_ocr and sin_texto and RE_DICE_TEXTO.search(desc):
                    for psm_ in (11, 6):
                        t2, _a2 = ocr_imagen(im['archivo'], psm=psm_, mejorar=True)
                        t2 = limpiar_ocr(t2, 600)
                        if len(re.sub(r'\W+', '', t2)) >= 15:
                            txt, sin_texto = t2, False
                            log('#%d img%d: la descripción menciona texto: reintentado con tesseract (modo %d) y se ha leído.' % (i, k, psm_))
                            break
            if not desc and (im['alt'] or '').strip() and (sin_texto or siempre_desc):
                desc = 'Facebook la describe así: ' + im['alt'].strip()
            con.execute('UPDATE imagenes SET ocr=?, descripcion=? WHERE id=?', ((txt or '').strip(), desc, im['id']))
            con.commit()
            hechas += 1
            log('#%d %s img%d:' % (i, p['autor'] or '', k))
            log(envolver('Texto: ' + (' '.join((txt or '').split()) or '(no se detecta texto)')))
            if desc:
                log(envolver('Se ve: ' + ' '.join(desc.split())))
            elif sin_texto and not hay_ia:
                log('    (sin texto y sin modelo de visión local: no se puede decir qué se ve; mira: python3 fb.py ia)')
            if aviso:
                log('    (' + aviso + ')')
    return hechas


def cmd_imgtexto(a):
    """Texto (OCR) y descripción de las imágenes de publicaciones (1 imagen; --todas para todas las de cada una)."""
    con = db()
    if a.pendientes:
        ids = [r[0] for r in con.execute("""SELECT DISTINCT p.id FROM publicaciones p JOIN imagenes i ON i.pub_id=p.id
                   WHERE i.estado='ok' AND (i.ocr IS NULL OR (COALESCE(i.descripcion,'')='' AND ?)) ORDER BY p.id DESC LIMIT ?""",
                                  (1 if hay_vision() else 0, a.n or 30)).fetchall()]
        if not ids:
            sys.exit('No hay imágenes pendientes de leer.')
        a.todas = True
    else:
        ids = pubs_de_args(con, a)
    nf = int(getattr(a, 'n_frames', None) if getattr(a, 'n_frames', None) is not None else (config().get('fotogramas_video') or 0))
    if nf > 0:
        for i in ids:
            if tiene_video(con, i) and not con.execute("SELECT 1 FROM imagenes WHERE pub_id=? AND url LIKE 'fotograma:%'", (i,)).fetchone():
                print('#%d tiene vídeo: sacando %d fotogramas...' % (i, nf))
                sacar_fotogramas(con, i, nf, float(config().get('video_segundos') or 5))
        a.todas = True
    por_fuente = {}
    for i in ids:
        p = con.execute('SELECT fuente_id FROM publicaciones WHERE id=?', (i,)).fetchone()
        if p:
            por_fuente[p['fuente_id']] = 1
    for fid in por_fuente:
        descargar_pendientes(fid)
    n = analizar_imagenes(ids, todas=a.todas, rehacer=a.rehacer, siempre_desc=a.descripcion or a.todas,
                          limite=int(config().get('describir_max') or 0), fotogramas=a.todas)
    print('\nImágenes procesadas: %d. Se guardan y salen en el apartado «Lo que hay en las imágenes» al ver la publicación:  python3 fb.py post Nº' % n)


def cmd_copiar(a):
    """Copia a UNA carpeta todos los archivos (imágenes y fotogramas) de varias publicaciones, más un índice de texto."""
    con = db()
    ids = pubs_de_args(con, a, 10)
    if a.con_original:
        for i in list(ids):
            o = con.execute('SELECT original_id FROM publicaciones WHERE id=?', (i,)).fetchone()
            if o and o['original_id'] and o['original_id'] not in ids:
                ids.append(o['original_id'])
    fids = {r['fuente_id'] for r in con.execute('SELECT fuente_id FROM publicaciones WHERE id IN (%s)' % ','.join(str(i) for i in ids)).fetchall()}
    for fid in fids:
        descargar_pendientes(fid)
    con = db()
    if a.a:
        dest = os.path.expanduser(a.a)
    else:
        base = os.path.dirname(dir_imagenes().rstrip('/')) if os.path.isdir(os.path.dirname(dir_imagenes().rstrip('/'))) else ruta('')
        dest = os.path.join(base, 'copias', datetime.now().strftime('%Y-%m-%d-%H-%M-%S'))
    os.makedirs(dest, exist_ok=True)
    n = 0
    indice = ['FB Vigía - copia de %d publicaciones (%s)' % (len(ids), ahora()), '']
    for i in ids:
        p = con.execute('SELECT p.*, f.nombre AS fn FROM publicaciones p JOIN fuentes f ON f.id=p.fuente_id WHERE p.id=?', (i,)).fetchone()
        if not p:
            continue
        indice += ['#%d %s | %s | %s' % (p['id'], p['fn'], p['autor'] or '', p['fecha_aprox'] or p['tiempo_texto'] or ''), (p['texto'] or '').strip(), p['permalink'] or '']
        if p['compartido_de']:
            indice.append('Compartida de: %s %s' % (p['compartido_de'], p['compartido_url'] or ''))
        for im in con.execute("SELECT * FROM imagenes WHERE pub_id=? AND estado='ok' ORDER BY id", (i,)).fetchall():
            if not im['archivo'] or not os.path.exists(im['archivo']):
                continue
            d = os.path.join(dest, os.path.basename(im['archivo']))
            if os.path.exists(d) and not a.sobrescribir:
                continue
            shutil.copy2(im['archivo'], d)
            n += 1
            indice.append('  archivo: ' + os.path.basename(d))
            if (im['ocr'] or '').strip():
                indice.append('    texto: ' + ' '.join(im['ocr'].split()))
            if (im['descripcion'] or '').strip():
                indice.append('    se ve: ' + ' '.join(im['descripcion'].split()))
        indice.append('')
    with open(os.path.join(dest, 'INDICE.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(indice))
    print('Copiados %d archivos de %d publicaciones a:\n   %s\n(con un INDICE.txt con el texto de cada publicación)' % (n, len(ids), os.path.realpath(dest)))


def url_original(p):
    """Dirección de la publicación ORIGINAL de una compartida (None si no se puede deducir)."""
    u = (p['compartido_url'] or '').strip()
    if not u:
        return None
    pg0 = pagina_de_post_url(u)
    if pg0:
        if urlparse(u).path.startswith('/photo') and pg0['tipo'] == 'grupo':   # foto de un grupo: se abre la publicación (groups/G/posts/N), no el visor
            return '%s/posts/%s' % (pg0['url'], pg0['id'])
        return u
    m = re.match(r'^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:photo/?\?|photo\.php\?)(.*)$', u)
    if m:
        q = parse_qs(m.group(1))
        fb_ = (q.get('fbid') or [''])[0]
        pu = urlparse(p['compartido_autor_url'] or '')
        seg = [s for s in pu.path.split('/') if s]
        if seg and seg[0] not in ('groups', 'people', 'pages', 'profile.php', 'photo') and fb_.isdigit():
            return 'https://www.facebook.com/%s/posts/%s' % (seg[0], fb_)
        if pu.path.startswith('/profile.php') and fb_.isdigit():
            return 'https://www.facebook.com/permalink.php?story_fbid=%s&id=%s' % (fb_, (parse_qs(pu.query).get('id') or [''])[0])
    return None


def cmd_original(a):
    """De una publicación que COMPARTE otra: lee la publicación original (texto, reacciones, comentarios) y descarga sus imágenes."""
    con = db()
    ids = pubs_de_args(con, a, 5)
    for i in ids:
        p = con.execute('SELECT * FROM publicaciones WHERE id=?', (i,)).fetchone()
        if not p:
            print('#%s: no existe.' % i)
            continue
        if not p['compartido_de'] and not p['compartido_url']:
            print('#%d: no es una publicación compartida.' % i)
            continue
        u = url_original(p)
        if not u:
            if p['compartido_url']:
                print('#%d: su original es %s pero no sé convertirla en una dirección de publicación.' % (i, p['compartido_url']))
            else:
                print('#%d comparte una publicación de "%s", pero no se guardó el enlace de la original.' % (i, p['compartido_de']))
                print('     Vuelve a capturar esa página con la extensión 1.7.11 (python3 fb.py ultimas ... --releer) y repite.')
            continue
        print('#%d comparte la publicación de "%s": %s' % (i, p['compartido_de'], u))
        ns = argparse.Namespace(urls=[u], sin_detalle=a.sin_detalle, releer=a.releer, en_pagina=False, espera=a.espera, pausa=a.pausa, navegador=a.navegador)
        leidas = cmd_pub(ns) or []
        if leidas:
            con = db()
            con.execute('UPDATE publicaciones SET original_id=? WHERE id=?', (leidas[0], i))
            con.commit()
            fid = con.execute('SELECT fuente_id FROM publicaciones WHERE id=?', (leidas[0],)).fetchone()['fuente_id']
            descargar_pendientes(fid)
            con = db()
            p2 = con.execute('SELECT * FROM publicaciones WHERE id=?', (leidas[0],)).fetchone()
            for k, im in enumerate(con.execute("SELECT archivo, estado FROM imagenes WHERE pub_id=? ORDER BY id", (leidas[0],)).fetchall(), 1):
                print('    Imagen original %d: %s' % (k, os.path.realpath(im['archivo']) if im['estado'] == 'ok' and im['archivo'] else '(no descargada)'))
            print('La original quedó guardada como #%d (la ves con: python3 fb.py post %d -r -e 3)' % (leidas[0], leidas[0]))


def _ytdlp():
    if shutil.which('yt-dlp'):
        return ['yt-dlp']
    try:
        r = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'], capture_output=True, timeout=20)
        if r.returncode == 0:
            return [sys.executable, '-m', 'yt_dlp']
    except Exception:
        pass
    return None


def tiene_video(con, i):
    """¿La publicación lleva un vídeo/reel (su enlace o alguno de sus enlaces de Facebook lo es)?"""
    return bool(urls_video(con, i))


def urls_video(con, i):
    p = con.execute('SELECT permalink, compartido_url FROM publicaciones WHERE id=?', (i,)).fetchone()
    if not p:
        return []
    cands = []
    for u in [p['permalink'] or '', p['compartido_url'] or ''] + [r['url'] for r in con.execute("SELECT url FROM enlaces WHERE pub_id=? AND tipo='facebook'", (i,)).fetchall()]:
        if re.search(r'/(videos?|reel|watch)\b|/share/[rv]/|[?&]v=\d', u or '') and u not in cands:
            cands.append(u)
    return cands


def sacar_fotogramas(con, i, n=3, seg=5.0, cookies='', forzar=False, log=print):
    """Baja los primeros segundos del vídeo de la publicación i y guarda n fotogramas como imágenes de la publicación. Devuelve las rutas."""
    yt = _ytdlp()
    ff = shutil.which('ffmpeg')
    if not yt or not ff:
        log('Hace falta:  pkg install ffmpeg   y   pip install yt-dlp' + ('' if yt else '   (falta yt-dlp)') + ('' if ff else '   (falta ffmpeg)'))
        return []
    n = max(1, min(int(n), 20))
    seg = max(1.0, float(seg))
    import tempfile
    p = con.execute('SELECT p.*, f.clave AS fclave, f.nombre AS fnombre FROM publicaciones p JOIN fuentes f ON f.id=p.fuente_id WHERE p.id=?', (i,)).fetchone()
    if not p:
        log('#%s: no existe.' % i)
        return []
    cands = urls_video(con, i)
    if not cands and forzar and p['permalink']:
        cands = [p['permalink']]
    if not cands:
        log('#%d: no parece tener vídeo (su enlace no es de vídeo). Si lo tiene, prueba con --forzar.' % i)
        return []
    tmp = tempfile.mkdtemp(prefix='fbvid_')
    arch = None
    err = ''
    for u in cands:
        cmd = yt + ['--no-playlist', '-q', '--no-warnings', '-f', 'worst[ext=mp4]/worst', '--download-sections', '*0-%d' % int(seg + 2), '-o', os.path.join(tmp, 'v.%(ext)s'), u]
        if cookies:
            cmd[len(yt):len(yt)] = ['--cookies', os.path.expanduser(cookies)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            err = (r.stderr or r.stdout or '').strip().splitlines()[-1:] or ['']
            err = err[0]
        except Exception as e:
            err = str(e)
        fs = [x for x in os.listdir(tmp) if x.startswith('v.')]
        if fs:
            arch = os.path.join(tmp, fs[0])
            break
    if not arch:
        log('#%d: no se pudo bajar el vídeo (%s).' % (i, err[:150]))
        log('     Los vídeos de grupos privados o que exigen sesión necesitan --cookies archivo.txt (cookies de Facebook exportadas de Firefox).')
        portada = con.execute("SELECT 1 FROM imagenes WHERE pub_id=? AND alt LIKE 'portada del v%'", (i,)).fetchone()
        if portada:
            log('     Se usará la imagen de PORTADA del vídeo que ya se guardó con la publicación.')
        shutil.rmtree(tmp, ignore_errors=True)
        return []
    carpeta = os.path.join(dir_imagenes(), slug(p['fclave'], 60))
    os.makedirs(carpeta, exist_ok=True)
    base = nombre_imagen(p['fecha_aprox'], p['primera_vez'], p['fnombre'], p['fclave'], p['clave'], 0).rsplit('-img', 1)[0]
    hechas = []
    for k in range(n):
        t = seg * (k + 0.5) / n if n > 1 else min(1.0, seg / 2)
        out = os.path.join(carpeta, '%s-video-f%d-%ds.jpg' % (base, k + 1, int(t)))
        r = subprocess.run([ff, '-y', '-loglevel', 'error', '-ss', '%.2f' % t, '-i', arch, '-frames:v', '1', '-q:v', '2', out], capture_output=True, text=True, timeout=120)
        if r.returncode == 0 and os.path.exists(out):
            hechas.append(out)
            con.execute('INSERT OR IGNORE INTO imagenes(pub_id,url,norm,alt,enlace,archivo,estado) VALUES(?,?,?,?,?,?,?)',
                        (i, 'fotograma:%d' % int(t), 'fotograma:%s:%d' % (cands[0], k + 1), 'fotograma del vídeo (s %d)' % int(t), cands[0], out, 'ok'))
    con.commit()
    shutil.rmtree(tmp, ignore_errors=True)
    log('#%d %s: %d fotogramas de los primeros %d s.' % (i, p['autor'] or '', len(hechas), int(seg)))
    return hechas


def cmd_video(a):
    """Saca 1, 2, 5... fotogramas de los primeros segundos del vídeo de una publicación y los guarda como imágenes."""
    con = db()
    ids = pubs_de_args(con, a, 3)
    if not _ytdlp() or not shutil.which('ffmpeg'):
        sacar_fotogramas(con, ids[0], 1)   # muestra qué falta instalar
        return
    for i in ids:
        hechas = sacar_fotogramas(con, i, a.n_fotos, a.segundos, a.cookies, a.forzar)
        for h in hechas:
            print('    ' + os.path.realpath(h))
        if not hechas:
            print('    (no se sacó ningún fotograma)')
    if getattr(a, 'describir', False):
        analizar_imagenes(ids, todas=True, siempre_desc=True, fotogramas=True, rehacer=False)
    else:
        print('Para leerlos y describirlos:  python3 fb.py imgtexto %s --todas' % ' '.join(str(x) for x in ids))


def cmd_imagenes(a):
    con = db()
    f = buscar_fuente(con, a.fuente) if a.fuente else None
    if getattr(a, 'renombrar', False):
        print('Imágenes renombradas con el formato nuevo: %d' % renombrar_imagenes(con, f['id'] if f else None))
    ok, err = descargar_pendientes(f['id'] if f else None)
    q = """SELECT i.id, f.nombre, p.autor, i.estado, i.archivo, i.enlace FROM imagenes i JOIN publicaciones p ON p.id=i.pub_id
           JOIN fuentes f ON f.id=p.fuente_id"""
    par = []
    if f:
        q += ' WHERE f.id=?'
        par.append(f['id'])
    q += ' ORDER BY i.id DESC LIMIT ?'
    par.append(a.n or 100000)
    filas = con.execute(q, par).fetchall()
    if not filas:
        print('No hay imágenes registradas.')
        return
    print(tabla_c(['Nº', 'Fuente', 'Autor', 'Estado', 'Archivo'], [[r['id'], r['nombre'], r['autor'], r['estado'], r['archivo'] or ''] for r in filas]))
    print('\nCarpeta de imágenes: %s' % os.path.realpath(dir_imagenes()))
    if not os.path.realpath(dir_imagenes()).startswith('/storage'):
        print('(Para que la Galería y otras apps las vean, ejecuta una vez: termux-setup-storage)')
    print('Las URL de imágenes de Facebook caducan: si salen con error, vuelve a capturar la página.')


def cmd_enlaces(a):
    con = db()
    q = """SELECT e.id, f.nombre, e.tipo, e.estado, e.url FROM enlaces e JOIN publicaciones p ON p.id=e.pub_id
           JOIN fuentes f ON f.id=p.fuente_id"""
    par = []
    if a.fuente:
        f = buscar_fuente(con, a.fuente)
        q += ' WHERE f.id=?'
        par.append(f['id'])
    q += ' ORDER BY e.id DESC LIMIT ?'
    par.append(a.n or 100000)
    filas = con.execute(q, par).fetchall()
    if not filas:
        print('No hay enlaces registrados.')
        return
    print(tabla_c(['Nº', 'Fuente', 'Tipo', 'Estado', 'URL'], [[r['id'], r['nombre'], r['tipo'], r['estado'], r['url'][:90]] for r in filas]))


def encolar(con, url, scrolls, origen='manual', accion='capturar', pub_id=None, vigilar=0):
    n = norm_url(url) if url else accion
    r = con.execute("SELECT id FROM cola WHERE norm=? AND COALESCE(accion,'capturar')=? AND estado IN ('pendiente','abriendo','en_curso')", (n, accion)).fetchone()
    if r:
        if vigilar:
            con.execute('UPDATE cola SET vigilar=1 WHERE id=?', (r['id'],))
        return None
    cur = con.execute('INSERT INTO cola(url,norm,scrolls,estado,creada,actualizada,origen,accion,pub_id,vigilar) VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (url or '', n, scrolls, 'pendiente', ahora(), ahora(), origen, accion, pub_id, 1 if vigilar else 0))
    return cur.lastrowid


def registrar_vigilada(con, url, nombre=''):
    """Pone en la lista de vigilados la página/grupo/persona de esa URL (la crea si aún no se había capturado)."""
    ck = clave_de_url(url)
    if not ck:
        return None
    f = fuente_por_clave(con, ck)
    if not f:
        nu = norm_url(url)
        for g in con.execute("SELECT * FROM fuentes WHERE url<>''").fetchall():
            if norm_url(g['url']) == nu:
                f = g
                break
    if f:
        poner_vigilada(con, f, 1)
        return f['id']
    nombre = limpia_nombre(nombre) or nombre_desde_listas(con, ck, url) or ck
    cur = con.execute('INSERT INTO fuentes(clave,tipo,nombre,url,creada,vigilada,bloqueada) VALUES(?,?,?,?,?,1,0)',
                      (ck, 'grupo' if ck.startswith('grupo-') else 'pagina', nombre, url, ahora()))
    return cur.lastrowid


def cmd_seguir(a):
    con = db()
    f = buscar_fuente(con, a.fuente)
    filas = con.execute("""SELECT e.* FROM enlaces e JOIN publicaciones p ON p.id=e.pub_id
                           WHERE p.fuente_id=? AND e.estado='pendiente' ORDER BY e.id DESC LIMIT ?""", (f['id'], a.max)).fetchall()
    if not filas:
        print('No hay enlaces pendientes de seguir en %s.' % f['nombre'])
        return
    ext = fbk = 0
    for e in filas:
        if e['tipo'] == 'facebook':
            encolar(con, e['url'], 3, 'seguir')
            con.execute("UPDATE enlaces SET estado='cola' WHERE id=?", (e['id'],))
            fbk += 1
            continue
        host = urlparse(e['url']).hostname or 'web'
        base = ruta('enlaces', slug(f['clave'], 50), '%05d_%s' % (e['id'], slug(host, 30)))
        os.makedirs(os.path.dirname(base), exist_ok=True)
        try:
            arch, titulo = seguir_enlace_externo(e['url'], base)
            con.execute("UPDATE enlaces SET estado='hecho', archivo=?, titulo=? WHERE id=?", (arch, titulo, e['id']))
            print('OK     %s\n       -> %s' % (e['url'][:90], arch))
            ext += 1
        except Exception as ex:
            con.execute("UPDATE enlaces SET estado=?, detalle=? WHERE id=?", ('error', str(ex)[:100], e['id']))
            print('ERROR  %s  (%s)' % (e['url'][:80], ex))
        con.commit()
    con.commit()
    print('\nEnlaces externos descargados: %d.  Enlaces de Facebook puestos en la cola de la extensión: %d.' % (ext, fbk))
    if fbk:
        print('La extensión los visitará sola (si Firefox está abierto) o ejecuta: python fb.py cola')


def cmd_vigilar(a):
    con = db()
    c = config()
    urls = []
    for u in (a.urls or []):
        if REF_RE.match(u):
            l = lista_por_ref(con, u)
            uu = url_de_lista(l)
            if uu:
                urls.append(uu)
            else:
                print('%s (%s) no tiene página de publicaciones que capturar.' % (u.upper(), l['nombre']))
        else:
            urls.append(expandir_url(u))
    if a.lista:
        t = TIPOS_LISTA.get(a.lista.lower())
        if not t or t in ('miembro', 'conversacion'):
            sys.exit('Lista no válida. Usa: contactos, paginas, grupos, amigos, personas')
        for r in con.execute("SELECT * FROM listas WHERE tipo=? AND baja IS NULL AND url<>'' ORDER BY nombre LIMIT ?", (t, a.max)).fetchall():
            uu = url_de_lista(r)
            fl = fuente_de_lista(con, r) if uu else None
            if uu and not (fl and fl['bloqueada']):
                urls.append(uu)
    if a.todas:
        for f in con.execute('SELECT * FROM fuentes WHERE vigilada=1 AND bloqueada=0 AND url<>\'\' ORDER BY id').fetchall():
            urls.append(f['url'])
    if a.inicio:
        urls.append('https://www.facebook.com/')
    if not urls:
        sys.exit('Indica una URL o una referencia (P3, G2, C5...), o --lista paginas, o --todas, o --inicio.')
    sc = a.scroll if a.scroll is not None else int(c['scrolls'])
    n = 0
    primera = None
    anadidas = 0
    for u in urls:
        antes = None
        if u.rstrip('/') != 'https://www.facebook.com':
            ck = clave_de_url(u)
            antes = fuente_por_clave(con, ck) if ck else None
            era = bool(antes and antes['vigilada'])
            fid = registrar_vigilada(con, u)
            if fid and not era:
                anadidas += 1
            if fid and a.recordar and a.scroll is not None:
                con.execute('UPDATE fuentes SET n_leer=? WHERE id=?', (a.scroll, fid))
                print('Guardado: de %s se leerán siempre %d publicaciones.' % (u, a.scroll))
        if a.sin_capturar:
            print('En vigilados: %s' % u)
            continue
        i = encolar(con, u, sc, 'abrir' if a.abrir else 'manual', vigilar=1)
        if i is None:
            print('Ya estaba en la cola: %s' % u)
        else:
            n += 1
            primera = primera or u
            print('En cola (#%d, %d publicaciones): %s' % (i, sc, u))
    con.commit()
    if a.abrir and primera:
        for cmd in (['termux-open-url', primera], ['am', 'start', '-a', 'android.intent.action.VIEW', '-d', primera]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print('Abriendo Firefox con la primera URL...')
                break
            except FileNotFoundError:
                continue
    tot = con.execute('SELECT COUNT(*) FROM fuentes WHERE vigilada=1 AND bloqueada=0').fetchone()[0]
    print('\nQUÉ HACE "vigilar": 1) las apunta en tu lista de VIGILADOS (ahora hay %d en total); 2) %s' % (
        tot, 'no las lee todavía (--sin-capturar).' if a.sin_capturar else 'las pone en la cola para leerlas YA (la extensión, con Firefox abierto, las irá visitando).'))
    print('"lanzar" abre SOLO tu lista de vigilados. Verla y cambiarla:  python fb.py vigilados')


def cmd_vigilados(a):
    """La lista de fuentes vigiladas (lo que abre 'lanzar'): verla, dejarla en las que quieras o vaciarla."""
    con = db()
    if a.ninguna or a.solo:
        con.execute('UPDATE fuentes SET vigilada=0 WHERE vigilada=1')
        if a.solo:
            for q in a.solo:
                f = buscar_fuente(con, q)
                poner_vigilada(con, f, 1)
        con.commit()
    filas = con.execute("""SELECT f.*, (SELECT COUNT(*) FROM publicaciones p WHERE p.fuente_id=f.id) npub FROM fuentes f
                           WHERE vigilada=1 AND bloqueada=0 ORDER BY id""").fetchall()
    if not filas:
        print('No hay ninguna fuente vigilada: "lanzar" no abriría nada.')
        print('Añade con:  python fb.py vigilar URL | P3 | G2 ...      (ver todo lo conocido: python fb.py fuentes)')
        return
    cats = categorias_fuentes(con)
    print('FUENTES VIGILADAS (esto es lo único que abre "python fb.py lanzar"): %d\n' % len(filas))
    print(tabla_c(['Nº', 'Tipo', 'Nombre', 'Public.', 'Leer', 'Última captura', 'URL'],
                [[f['id'], ','.join(sorted(cats.get(f['id'], {f['tipo']}))), f['nombre'], f['npub'], f['n_leer'] or '-', (f['ultima_captura'] or 'nunca')[:16], f['url']] for f in filas]))
    print('\nQuitar una:            python fb.py desvigilar Nº')
    print('Dejar solo algunas:    python fb.py vigilados --solo 5 8 12')
    print('Vaciar y empezar:      python fb.py vigilados --ninguna     (después: python fb.py vigilar ...)')
    print('Añadir:                python fb.py vigilar URL | P3 | G2     (con --sin-capturar solo la apunta)')
    print('Cantidad FIJA por fuente: python fb.py cantidad P3=30 G2=10 12=5   (se guarda para siempre; sin nada: tabla)   Solo esta vez: lanzar P3=30')
    print('Leer más de una:       python fb.py lanzar P141 -n 50    |   fijarlo:  python fb.py vigilar P141 --sin-capturar -n 50 --recordar')


LISTAS_URLS = [
    # (nombre, url, scrolls por defecto, qué recoge)
    ('inicio', 'https://www.facebook.com/', 8, 'contactos, grupos y páginas de accesos directos'),
    ('amigos', 'https://www.facebook.com/friends/list', 20, 'lista de amigos'),
    ('personas', 'https://www.facebook.com/me/following', 20, 'personas/páginas que sigues'),
    ('paginas', 'https://www.facebook.com/pages/?category=liked', 20, 'páginas que sigues'),
    ('grupos', 'https://www.facebook.com/groups/joins/', 15, 'tus grupos'),
    ('mensajes', 'https://www.facebook.com/messages/t/', 3, 'conversaciones recientes'),
]


def cmd_actualizar(a):
    """Pone en la cola todas las páginas de listas (y opcionalmente fuentes y conversaciones)."""
    con = db()
    solo = [x.strip() for x in a.solo.split(',')] if a.solo else None
    hechos = []
    for nombre, url, sc, que in LISTAS_URLS:
        if solo and nombre not in solo:
            continue
        hechos.append((nombre, url, a.scroll if a.scroll is not None else sc, que))
    if not solo or 'miembros' in solo:
        if not a.sin_miembros:
            for g in con.execute("SELECT nombre, url FROM listas WHERE tipo='grupo' AND baja IS NULL AND url<>'' ORDER BY nombre").fetchall():
                hechos.append(('miembros', g['url'].rstrip('/') + '/members', a.scroll_miembros, 'miembros de ' + (g['nombre'] or 'grupo')))
    if a.conversaciones:
        for c in con.execute("SELECT nombre, url FROM listas WHERE tipo='conversacion' AND url<>'' ORDER BY ultima_vez DESC LIMIT ?", (a.conversaciones,)).fetchall():
            hechos.append(('conversacion', c['url'], 10, 'conversación con ' + (c['nombre'] or '?')))
    if a.posts:
        for t in a.posts.split(','):
            tipo = TIPOS_LISTA.get(t.strip().lower())
            if not tipo or tipo in ('miembro', 'conversacion'):
                print('Tipo no válido para --posts: %s' % t)
                continue
            for r in con.execute("SELECT * FROM listas WHERE tipo=? AND baja IS NULL AND url<>'' ORDER BY nombre LIMIT ?", (tipo, a.max)).fetchall():
                u = url_de_lista(r)
                if u:
                    hechos.append(('posts', u, int(config()['scrolls']), r['nombre']))
    if a.fuentes:
        for f in con.execute("SELECT nombre, url FROM fuentes WHERE vigilada=1 AND url<>'' ORDER BY id").fetchall():
            hechos.append(('fuente', f['url'], int(config()['scrolls']), f['nombre']))
    nuevos = 0
    filas = []
    for nombre, url, sc, que in hechos:
        i = encolar(con, url, sc, 'manual')
        filas.append([nombre, sc, que, 'en cola' if i else 'ya estaba'])
        nuevos += 1 if i else 0
    con.commit()
    print(tabla(['Qué', 'Scroll', 'Contenido', 'Estado'], filas))
    seg = sum(h[2] for h in hechos) * 2 + len(hechos) * 8
    print('\n%d páginas en la cola (tiempo estimado: unos %d min).' % (nuevos, max(1, seg // 60)))
    print('La extensión las irá visitando una a una con Firefox abierto. Seguimiento: python fb.py cola')
    if not a.fuentes:
        print('Para actualizar también las páginas y grupos vigilados:  python fb.py actualizar --fuentes')
    if not con.execute("SELECT 1 FROM listas WHERE tipo='grupo'").fetchone() and not a.sin_miembros:
        print('Aún no conozco ningún grupo: los miembros se pedirán en la próxima actualización, cuando "inicio" o "grupos" los haya guardado.')


# ----------------------------------------------------------------------------
# abrir Firefox desde Termux y dejar que la extensión capture sola
# ----------------------------------------------------------------------------

PAQUETES_FIREFOX = ['org.mozilla.fenix', 'org.mozilla.firefox', 'org.mozilla.firefox_beta', 'org.mozilla.fennec_fdroid', 'org.mozilla.focus']


def servidor_activo():
    try:
        r = json.loads(urllib.request.urlopen('http://127.0.0.1:%d/ping' % int(config()['puerto']), timeout=3).read().decode('utf-8', 'replace'))
        return bool(r.get('version'))
    except Exception:
        return False


def quien_usa_puerto(puerto):
    """Qué hay en ese puerto: 'fbvigia', 'otro' (otro programa que responde), 'mudo' (acepta conexiones pero no responde) o 'libre'."""
    import socket
    s_ = socket.socket()
    s_.settimeout(1.5)
    try:
        s_.connect(('127.0.0.1', puerto))
    except OSError:
        return 'libre', ''
    finally:
        s_.close()
    try:
        raw = urllib.request.urlopen('http://127.0.0.1:%d/ping' % puerto, timeout=4).read().decode('utf-8', 'replace')
    except Exception as e:
        return 'mudo', str(e)[:80]
    try:
        j = json.loads(raw)
        if j.get('programa') == 'fbvigia' or (j.get('version') and 'fuentes' in j):
            return 'fbvigia', raw[:100]
    except Exception:
        pass
    return 'otro', raw[:100]


def puerto_libre_cerca(desde=8790):
    for p_ in range(desde, desde + 30):
        if quien_usa_puerto(p_)[0] == 'libre':
            return p_
    return None


def crear_servidor(puerto):
    """Crea el servidor HTTP; si el puerto está ocupado, explica quién lo usa y qué hacer (sin traceback)."""
    try:
        return ThreadingHTTPServer(('127.0.0.1', puerto), Manejador)
    except OSError as e:
        if getattr(e, 'errno', None) not in (98, 48, 10048):
            raise
        q, det = quien_usa_puerto(puerto)
        libre = puerto_libre_cerca(8790)
        print('\nEl puerto %d ya está ocupado, así que fb.py no puede arrancar su servidor.' % puerto)
        if q == 'fbvigia':
            print('Lo usa OTRO fb.py que ya está en marcha (respuesta: %s). No hace falta arrancar otro.' % det)
        elif q == 'mudo':
            print('Lo usa un programa que acepta conexiones pero no responde (%s): o un fb.py colgado (base de datos bloqueada) u otro programa tuyo.' % det)
            print('Si es un fb.py viejo:   pkill -9 -f fb.py     y vuelve a lanzar.')
        elif q == 'otro':
            print('Lo usa OTRO programa que responde (no es fb.py): %s' % det)
            print('(por ejemplo el puente de la extensión de SAMAR u otra herramienta tuya).')
        print('Para ver quién es:   ss -ltnp | grep %d      (o:  pgrep -af python)' % puerto)
        print('Para que ambos funcionen a la vez, dale a fb.py OTRO puerto:')
        print('   python3 fb.py puerto %d' % (libre or 8790))
        print('y pon el mismo en la extensión FB Vigía: icono de la extensión > «Servidor de Termux» = http://127.0.0.1:%d > «Guardar ajustes».' % (libre or 8790))
        sys.exit(1)


def asegurar_servidor():
    """Si el servidor no está en marcha, lo arranca en un hilo de este mismo proceso."""
    if servidor_activo():
        return False
    puerto = int(config()['puerto'])
    srv = crear_servidor(puerto)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print('(el servidor no estaba en marcha: lo arranco dentro de este comando, puerto %d)' % puerto)
    return True


def paquete_navegador(a=None):
    p = (getattr(a, 'navegador', None) or config().get('navegador') or '').strip()
    if p:
        return p
    try:
        r = subprocess.run(['pm', 'list', 'packages'], capture_output=True, text=True, timeout=15)
        pk = set(x.split(':', 1)[-1].strip() for x in r.stdout.splitlines())
        for c in PAQUETES_FIREFOX:
            if c in pk:
                return c
    except Exception:
        pass
    return 'org.mozilla.fenix'


def abrir_url(url, pkg):
    """Abre la URL en Firefox con 'am start'. Devuelve (ok, mensaje)."""
    cmd = ['am', 'start', '-a', 'android.intent.action.VIEW', '-d', url]
    if pkg:
        cmd += ['-p', pkg]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        sal = (r.stdout + r.stderr).strip()
        if r.returncode == 0 and 'Error' not in sal:
            return True, ''
        err = sal.splitlines()[-1] if sal else 'código %s' % r.returncode
    except FileNotFoundError:
        err = '"am" no existe (¿estás en Termux?)'
    except Exception as e:
        err = str(e)
    try:
        subprocess.run(['termux-open-url', url], capture_output=True, timeout=20)
        return True, 'con termux-open-url (am falló: %s)' % err
    except Exception:
        return False, err


def traer_firefox(pkg):
    """Pone Firefox al frente SIN abrir ninguna dirección (Android no dibuja las pestañas que no se ven: Facebook no pinta reacciones ni fechas)."""
    cmd = ['am', 'start', '-a', 'android.intent.action.MAIN', '-c', 'android.intent.category.LAUNCHER']
    if pkg:
        cmd += ['-p', pkg]
    try:
        subprocess.run(cmd, capture_output=True, timeout=15)
        return True
    except Exception:
        return False


def volver_a_termux():
    try:
        subprocess.run(['am', 'start', '-n', 'com.termux/com.termux.app.TermuxActivity'], capture_output=True, timeout=15)
    except Exception:
        pass


def lanzar_items(items, pkg, espera, pausa, volver=True):
    """items: lista de (url, accion, pub_id, scrolls, nombre). Abre cada URL en Firefox y espera a que la extensión termine."""
    resultados = []
    espera0 = espera
    traido = False
    try:   # que Android no duerma Termux mientras se espera (si la orden no existe, no pasa nada)
        subprocess.run(['termux-wake-lock'], timeout=5, capture_output=True)
    except Exception:
        pass
    for k, (url, accion, pub_id, sc, nombre) in enumerate(items, 1):
        # con muchas publicaciones hace falta más tiempo (~40 s por publicación leída con sus comentarios)
        espera = max(espera0, int(sc or 0) * 60 + 120) if accion == 'capturar' and 'facebook.com' in url else espera0
        con = db()
        n = norm_url(url)
        r = con.execute("SELECT id, estado FROM cola WHERE norm=? AND COALESCE(accion,'capturar')=? AND estado IN ('pendiente','abriendo','en_curso') ORDER BY id LIMIT 1",
                        (n, accion)).fetchone()
        ya_en_curso = False
        if r and r['estado'] == 'pendiente':
            cid = r['id']
            con.execute("UPDATE cola SET origen='abrir', creada=?, actualizada=?, scrolls=? WHERE id=?", (ahora(), ahora(), sc, cid))
        elif r:   # la extensión ya la tiene abierta (orden vieja): no se abre otra pestaña ni se reinicia
            cid = r['id']
            ya_en_curso = True
        else:
            cid = encolar(con, url, sc, 'abrir', accion, pub_id)
        con.commit()
        con.close()
        t0 = time.time()
        print('[%d/%d] %s ...' % (k, len(items), nombre or url[:60]), end=' ', flush=True)
        if ya_en_curso:
            print('(ya la está leyendo la extensión) ', end='', flush=True)
            ok, msg = True, ''
        else:
            ok, msg = abrir_url(url, pkg)
        if not ok:
            print('NO SE PUDO ABRIR (%s)' % msg)
            resultados.append((nombre, 'error', msg, cid))
            continue
        estado, det = 'tiempo', ''
        t_traer = 0
        while True:
            time.sleep(3)
            con = db()
            r = con.execute('SELECT estado, detalle, actualizada, oculta FROM cola WHERE id=?', (cid,)).fetchone()
            con.close()
            if r and r['oculta'] and r['estado'] in ('abriendo', 'en_curso') and time.time() - t_traer > 25 and not os.environ.get('FB_NO_TRAER'):
                # la pestaña quedó oculta (Termux delante): Firefox no pinta lo que no se ve. Se trae al frente y se sigue esperando.
                t_traer = time.time()
                if traer_firefox(pkg):
                    traido = True
                    print('(Firefox estaba en segundo plano: lo traigo al frente) ', end='', flush=True)
            if r and r['estado'] in ('hecha', 'error'):
                estado, det = r['estado'], r['detalle'] or ''
                break
            # pasado el tiempo previsto, se sigue esperando mientras la extensión dé señales de vida (latidos cada ~20 s)
            if time.time() - t0 >= espera and not (r and r['estado'] == 'en_curso' and delta_seg(r['actualizada']) < 240):
                break
        if estado == 'tiempo':
            con = db()
            con.execute("UPDATE cola SET estado='error', detalle='no la capturó a tiempo (¿Firefox cerrado o extensión sin sondeo?)', actualizada=? WHERE id=? AND estado<>'hecha'", (ahora(), cid))
            con.commit()
            con.close()
            det = 'sin respuesta en %d s' % espera
        print('%s %s (%d s)' % ('OK' if estado == 'hecha' else 'FALLO', det, time.time() - t0))
        resultados.append((nombre, estado, det, cid))
        if k < len(items):
            time.sleep(pausa)
    try:
        subprocess.run(['termux-wake-unlock'], timeout=5, capture_output=True)
    except Exception:
        pass
    if volver or traido:
        volver_a_termux()
    return resultados


def edad_hash(p):
    """'&fbv-edad=DÍAS' según la fecha guardada de la publicación: la extensión espera más cuanto más antigua es."""
    out = ''
    try:
        f = (p['fecha_aprox'] or '')[:10]
        if f:
            d = (datetime.now().date() - datetime.strptime(f, '%Y-%m-%d').date()).days
            if d > 0:
                out += '&fbv-edad=%d' % d
    except Exception:
        pass
    try:   # principio del texto guardado: con él la extensión reconoce la tarjeta aunque cambie su clave o el pfbid del enlace
        t = re.sub(r'\s+', ' ', (p['texto'] or p['texto_compartido'] or '')).strip()[:70]
        if len(t) >= 25:
            out += '&fbv-txt=' + quote(t, safe='')
    except Exception:
        pass
    return out


def cmd_cortadas(a):
    """Publicaciones cuyo texto se guardó CORTADO (o puede estarlo) y cómo hacer que se relean."""
    con = db()
    filas = con.execute("SELECT p.id, p.autor, p.texto, p.texto_cortado, p.leida_dia FROM publicaciones p WHERE COALESCE(p.texto_cortado,0)=1 ORDER BY p.id DESC").fetchall()
    sosp = []
    if a.sospechosas:
        for r in con.execute("SELECT id, autor, texto, texto_cortado, leida_dia FROM publicaciones WHERE COALESCE(texto_cortado,0)=0 AND length(COALESCE(texto,''))>=80 ORDER BY id DESC LIMIT 3000").fetchall():
            t = (r['texto'] or '').rstrip()
            if t and not re.search(r'[.!?…:;»"”)\]\u2600-\U0001fAFF]$', t) and not re.search(r'#\w+$|https?://\S+$', t):
                sosp.append(r)
    print('Con el texto marcado como CORTADO: %d' % len(filas))
    for r in filas[:a.n]:
        print('  #%d %s: ...%s' % (r['id'], r['autor'] or '', ' '.join((r['texto'] or '').split())[-70:]))
    if a.sospechosas:
        print('\nTextos largos que NO acaban en punto ni en signo (pueden estar cortados; es solo una pista): %d' % len(sosp))
        for r in sosp[:a.n]:
            print('  #%d %s: ...%s' % (r['id'], r['autor'] or '', ' '.join((r['texto'] or '').split())[-70:]))
    if a.marcar:
        ids = [r['id'] for r in filas] + [r['id'] for r in sosp]
        for i in ids:
            con.execute('UPDATE publicaciones SET texto_cortado=1, leida_dia=NULL WHERE id=?', (i,))
        con.commit()
        print('\nMarcadas %d: en el próximo  lanzar  se vuelven a leer a fondo (y se completa el texto).' % len(ids))
    elif filas or sosp:
        print('\nPara que se relean en el próximo lanzar:  python3 fb.py cortadas --marcar' + ('  (con --sospechosas se incluyen las dudosas)' if not a.sospechosas else ''))


def cmd_cantidad(a):
    """Fija cuántas publicaciones NUEVAS se recogen de cada fuente vigilada cada vez que se lanza."""
    con = db()
    cambios = []
    if a.todas is not None:
        for f in con.execute('SELECT id FROM fuentes WHERE vigilada=1 AND bloqueada=0').fetchall():
            cambios.append((f['id'], a.todas))
    i = 0
    toks = list(a.pares or [])
    while i < len(toks):
        t = toks[i]
        m = re.match(r'^(.+?)[=:](\d+)$', t)
        if m:
            q, n = m.group(1), int(m.group(2))
            i += 1
        elif i + 1 < len(toks) and toks[i + 1].isdigit():
            q, n = t, int(toks[i + 1])
            i += 2
        else:
            sys.exit('No entiendo "%s". Formato:  python fb.py cantidad P3=30 G2=10 12=5   (o  --todas 20)' % t)
        f = buscar_fuente(con, q, crear=True)
        cambios.append((f['id'], n))
    if not cambios:
        filas = con.execute('SELECT * FROM fuentes WHERE bloqueada=0 AND (vigilada=1 OR n_leer IS NOT NULL) ORDER BY id').fetchall()
        print('Publicaciones que se leen de cada fuente al lanzar (por defecto %d):\n' % int(config()['scrolls']))
        print(tabla_c(['Nº', 'Nombre', 'Se leen', 'Vigilada'], [[f['id'], f['nombre'], f['n_leer'] or ('%d (por defecto)' % int(config()['scrolls'])), 'sí' if f['vigilada'] else 'no (solo con lanzar %s)' % f['clave'][:20]] for f in filas]))
        print('\nCambiar:  python fb.py cantidad P3=30 G2=10 12=5      Todas:  python fb.py cantidad --todas 20      Volver al valor por defecto:  P3=0')
        return
    for fid, n in cambios:
        con.execute('UPDATE fuentes SET n_leer=? WHERE id=?', (n or None, fid))
        nombre = con.execute('SELECT nombre FROM fuentes WHERE id=?', (fid,)).fetchone()['nombre']
        print('%s (Nº %d): %s' % (nombre, fid, ('%d publicaciones cada vez que se lance' % n) if n else 'valor por defecto (%d)' % int(config()['scrolls'])))
    con.commit()
    print('Las publicaciones ya leídas a fondo hoy cuentan en ese número, pero no se vuelven a leer enteras: solo se actualizan sus números\ny, si tienen comentarios nuevos, se leen esos.')


def cmd_lanzar(a):
    """Abre en Firefox las páginas vigiladas (o las indicadas) y la extensión las captura sola y cierra las pestañas."""
    con = db()
    t_ini = ahora()
    cfgsc = int(config()['scrolls'])
    sc = a.scroll if a.scroll is not None else cfgsc
    items = []
    vistos = set()

    def anadir(url, nombre, scx=None):
        n = norm_url(url)
        if url and n not in vistos:
            vistos.add(n)
            if getattr(a, 'releer', False):
                url = url.split('#')[0] + '#fbv-releer=1'
            items.append((url, 'capturar', None, scx if scx is not None else sc, nombre))

    if a.objetivos:
        for u in a.objetivos:
            mN = re.match(r'^(.+?)[=:](\d+)$', u)
            nx = None
            if mN and (REF_RE.match(mN.group(1)) or mN.group(1).isdigit()):
                u, nx = mN.group(1), int(mN.group(2))   # P141=50 (o P141:50) = esa página, 50 publicaciones solo esta vez
            if REF_RE.match(u):
                l = lista_por_ref(con, u)
                uu = url_de_lista(l)
                if uu:
                    fl = fuente_de_lista(con, l)
                    anadir(uu, '%s %s' % (u.upper(), l['nombre']), nx if nx is not None else (a.scroll if a.scroll is not None else ((fl['n_leer'] or None) if fl else None)))
                else:
                    print('%s (%s) no tiene página que abrir.' % (u.upper(), l['nombre']))
            elif u.isdigit():
                f = buscar_fuente(con, u)
                anadir(f['url'], f['nombre'], nx if nx is not None else (a.scroll if a.scroll is not None else (f['n_leer'] or None)))
            else:
                anadir(expandir_url(u), u[:60], nx)
    else:
        de = None
        if a.de:
            de = {x.strip().lower() for x in a.de.split(',') if x.strip()}
        cats = categorias_fuentes(con) if de else {}
        omitidas = []
        usadas = set()
        for f in con.execute("SELECT * FROM fuentes WHERE vigilada=1 AND bloqueada=0 AND url<>'' ORDER BY id").fetchall():
            if de and not (cats.get(f['id'], set()) & de):
                continue
            if f['id'] in usadas:
                continue
            grupo = misma_fuente(con, f)
            if any(con.execute('SELECT bloqueada FROM fuentes WHERE id=?', (i,)).fetchone()['bloqueada'] for i in grupo):
                omitidas.append('#%d %s (su duplicada está desvigilada)' % (f['id'], f['nombre']))
                usadas |= grupo
                continue
            if len(grupo) > 1:
                otros = sorted(grupo - {f['id']} - usadas)
                if otros:
                    omitidas.append('#%s (duplicada de #%d %s)' % (', #'.join(str(i) for i in otros), f['id'], f['nombre']))
            usadas |= grupo
            anadir(f['url'], f['nombre'], (f['n_leer'] or None) if a.scroll is None else None)
        if omitidas:
            print('No se piden dos veces: ' + '; '.join(omitidas) + '.\n   (Para juntarlas del todo: python fb.py duplicadas --unir)\n')
    if a.listas:
        for nombre, url, scl, que in LISTAS_URLS:
            anadir(url, 'lista ' + nombre, scl)
    if a.max and len(items) > a.max:
        items = items[:a.max]
    ncan = limpiar_cola_obsoleta(con)
    con.commit()
    if ncan:
        print('Anuladas %d órdenes antiguas de la cola que eran de fuentes desvigiladas.' % ncan)
    prop = {norm_url(u) for u, *_ in items}
    viejas = [r for r in con.execute("SELECT norm FROM cola WHERE estado='pendiente' AND COALESCE(accion,'capturar')='capturar'").fetchall() if r['norm'] not in prop]
    if viejas:
        print('Aviso: hay %d órdenes antiguas pendientes en la cola que la extensión también ejecutará por su cuenta.' % len(viejas))
        print('   Verlas: python fb.py cola     Quitarlas todas: python fb.py cola vaciar\n')
    if not items:
        sys.exit('No hay nada que abrir: no hay fuentes vigiladas. Usa: python fb.py lanzar URL...  o  python fb.py vigilar URL  antes.')
    pkg = paquete_navegador(a)
    print('Voy a abrir %d página(s) en %s, una tras otra (espera máx. %d s cada una, más si piden muchas publicaciones; mientras la extensión dé señales, sigue esperando):' % (len(items), pkg, a.espera))
    for k, it in enumerate(items, 1):
        print('   %2d. %s   %s   [%s publicaciones]' % (k, it[4] or '', it[0], it[3]))
    if not a.objetivos:
        print('   (son tus fuentes VIGILADAS; para cambiarlas: python fb.py vigilados)')
    print('Mantén Firefox instalado con la extensión; la pantalla puede apagarse, pero conviene: termux-wake-lock\n')
    con.close()
    asegurar_servidor()
    res = lanzar_items(items, pkg, a.espera, a.pausa, volver=not a.sin_volver)
    bien = sum(1 for r in res if r[1] == 'hecha')
    print('\nTerminado: %d de %d capturadas.' % (bien, len(res)))
    if bien:
        print('Novedades:')
        cmd_nuevas(argparse.Namespace(n=0, resumen=True, no_marcar=True))
        c2 = db()
        vacias = c2.execute("""SELECT DISTINCT f.nombre FROM capturas c JOIN fuentes f ON f.id=c.fuente_id
                              WHERE c.fecha>=? AND c.n_posts=0 AND f.tipo IN ('pagina','grupo','persona_grupo')
                              AND c.fuente_id NOT IN (SELECT fuente_id FROM capturas WHERE fecha>=? AND n_posts>0)""", (t_ini, t_ini)).fetchall()
        if vacias:
            print('\nSin publicaciones visibles (perfil privado, sin contenido o sin permiso): ' + '; '.join(v['nombre'] for v in vacias))
        c2.close()
        print('\nLéelas con: python fb.py nuevas   (o: python fb.py ultimas -n 20)')
        print('¿Falta alguien por vigilar?  python fb.py sugerir')
        if (a.describir or config().get('describir_imagenes')) and not a.no_describir:
            c3 = db()
            ids3 = [r[0] for r in c3.execute("""SELECT DISTINCT p.id FROM publicaciones p JOIN imagenes i ON i.pub_id=p.id
                      WHERE p.ultima_vez>=? ORDER BY p.id""", (t_ini,)).fetchall()]
            nf3 = int(a.n_frames if getattr(a, 'n_frames', None) is not None else (config().get('fotogramas_video') or 0))
            if nf3 > 0:   # vídeos/reels: se sacan N fotogramas de cada uno (aunque solo tuvieran la portada)
                for r in c3.execute("SELECT id FROM publicaciones WHERE ultima_vez>=? ORDER BY id", (t_ini,)).fetchall():
                    if tiene_video(c3, r[0]) and not c3.execute("SELECT 1 FROM imagenes WHERE pub_id=? AND url LIKE 'fotograma:%'", (r[0],)).fetchone():
                        print('#%d tiene vídeo: sacando %d fotogramas...' % (r[0], nf3))
                        sacar_fotogramas(c3, r[0], nf3, float(config().get('video_segundos') or 5))
                ids3 = sorted({r[0] for r in c3.execute("SELECT DISTINCT pub_id FROM imagenes WHERE pub_id IN (SELECT id FROM publicaciones WHERE ultima_vez>=?)", (t_ini,)).fetchall()})
            c3.close()
            if ids3:
                print('\nLeyendo y describiendo las imágenes de %d publicaciones (puede tardar; Ctrl+C para parar, lo hecho queda guardado)...' % len(ids3))
                descargar_pendientes()
                try:
                    n3 = analizar_imagenes(ids3, todas=True, siempre_desc=True, limite=int(config().get('describir_max') or 0), fotogramas=True)
                    print('Imágenes leídas/descritas: %d. Mira el apartado «Lo que hay en las imágenes» con: python3 fb.py nuevas  o  post Nº' % n3)
                except KeyboardInterrupt:
                    print('\nInterrumpido; lo ya hecho está guardado (el resto: python3 fb.py imgtexto --pendientes).')
    if bien < len(res):
        print('\nLas que fallaron: python fb.py cola   (reintentar: python fb.py cola reintentar)')
        print('Si NINGUNA se abrió: Ajustes de Android > Apps > Termux > "Mostrar sobre otras apps" = permitido.')


def cmd_abiertas(a):
    """Pide a la extensión que capture todas las pestañas de Facebook que ya estén abiertas."""
    con = db()
    i = encolar(con, '', a.scroll if a.scroll is not None else int(config()['scrolls']), 'manual', 'abiertas')
    con.commit()
    if i is None:
        print('Ya había una orden pendiente de capturar las pestañas abiertas.')
    else:
        print('Orden enviada (#%d): la extensión capturará las pestañas de Facebook abiertas (en menos de 1 min).' % i)
    print('Requiere el servidor activo y Firefox abierto. Seguimiento: python fb.py cola')


def url_post_de_foto(con, p):
    """Facebook enlaza las publicaciones de FOTO con la foto (facebook.com/photo/?fbid=...), que abre el visor y no la publicación.
    Se calcula la dirección de la publicación: PÁGINA/posts/NÚMERO, con NÚMERO = el de «set=pcb.NÚMERO» (varias fotos) o el de la foto."""
    u = p['permalink'] or ''
    m = re.match(r'^https?://(?:www\.|m\.|web\.)?facebook\.com/(?:photo/?\?|photo\.php\?)(.*)$', u)
    if not m:
        return u
    q = parse_qs(m.group(1))
    num = ''
    sset = (q.get('set') or [''])[0]
    mm = re.match(r'^pcb\.(\d+)$', sset)
    if mm:
        num = mm.group(1)
    elif (q.get('fbid') or [''])[0].isdigit():
        num = q['fbid'][0]
    f = con.execute('SELECT url FROM fuentes WHERE id=?', (p['fuente_id'],)).fetchone()
    pu = urlparse((f['url'] if f else '') or p['autor_url'] or '')
    slug = ''
    if pu.path.strip('/') and not pu.path.startswith('/profile.php') and pu.path.split('/')[1] not in ('groups', 'pages', 'people', 'photo'):
        slug = pu.path.strip('/').split('/')[0]
    elif pu.path.startswith('/profile.php'):
        slug = (parse_qs(pu.query).get('id') or [''])[0]
    if not (num and slug):
        return u
    return 'https://www.facebook.com/%s/posts/%s' % (slug, num)


RE_REEL = re.compile(r'/reels?/\d{6,}')


def url_reel(con, p):
    """Dirección del REEL de Facebook que lleva la publicación (None si no es un reel). Mira su enlace y los enlaces guardados."""
    cand = [p['permalink'] or '']
    try:
        if 'compartido_url' in p.keys():
            cand.append(p['compartido_url'] or '')
    except Exception:
        pass
    cand += [r[0] or '' for r in con.execute('SELECT url FROM enlaces WHERE pub_id=?', (p['id'],)).fetchall()]
    for u in cand:
        if RE_REEL.search(u):
            return u.split('#')[0]
    return None


def aviso_reel(i, u):
    """i = número de publicación (o None si solo se tiene la dirección)."""
    print(('#%s: es un REEL de Facebook (%s). NO se abre.' % (i, u)) if i else ('Esta dirección es un REEL de Facebook y NO se abre: %s' % u))
    print(envolver('Un reel se abre en un visor de pantalla completa que no es el de una publicación normal: ahí la extensión no está preparada (ni probada) '
                   'para leer quién reaccionó, compartió o comentó, y el reel puede no cargar. Lo que Facebook enseña del reel en la lista de la página o '
                   'grupo (texto, número de reacciones y comentarios) ya se guarda con  lanzar.'))
    print('    Para intentarlo igualmente:  --forzar' + ('      Para sacar imágenes del vídeo:  python3 fb.py video %s' % i if i else ''))


def estado_detalle(con, pid):
    """Cuánto se ha conseguido ya del detalle de una publicación y si falta algo (para ir completándolo pasada a pasada)."""
    p = con.execute('SELECT * FROM publicaciones WHERE id=?', (pid,)).fetchone()
    if not p:
        return None
    nr = con.execute('SELECT COUNT(*) FROM reaccionantes WHERE pub_id=?', (pid,)).fetchone()[0]
    ns = con.execute('SELECT COUNT(*) FROM compartidores WHERE pub_id=?', (pid,)).fetchone()[0]
    nc = con.execute('SELECT COUNT(*) FROM comentarios WHERE pub_id=?', (pid,)).fetchone()[0]
    try:
        hist = json.loads(p['detalle_hist'] or '[]')
    except Exception:
        hist = []
    pasada = int(p['detalle_pasada'] or 0)
    er, ec = int(p['reacciones'] or 0), int(p['ncomentarios'] or 0)
    limitado = bool(p['detalle_limitado'])
    r_ok = limitado or er == 0 or nr >= 0.95 * er
    c_ok = ec == 0 or nc >= 0.95 * ec
    t_ok = not p['texto_cortado']
    estancado = len(hist) >= 3 and all(h.get('nuevos', 1) == 0 for h in hist[-2:])   # dos pasadas seguidas sin traer nada nuevo: Facebook no da más
    completo = pasada >= 1 and ((r_ok and c_ok and t_ok) or estancado)
    return {'p': p, 'pasada': pasada, 'nr': nr, 'ns': ns, 'nc': nc, 'er': er, 'ec': ec, 'esp_s': int(p['ncompartidos'] or 0),
            'r_ok': r_ok, 'c_ok': c_ok, 't_ok': t_ok, 'estancado': estancado, 'completo': completo, 'limitado': limitado, 'hist': hist}


def registrar_pasada_detalle(con, pid, d, t0):
    """Anota una pasada de detalle: su número, cuánto hay ahora guardado y cuánto se añadió; completa el texto si ahora viene más largo."""
    p = con.execute('SELECT * FROM publicaciones WHERE id=?', (pid,)).fetchone()
    if not p:
        return
    antes = estado_detalle(con, pid)
    pasada = int(d.get('pasada') or 0) or (int(p['detalle_pasada'] or 0) + 1)
    hist = antes['hist']
    prev = hist[-1] if hist else {'r': 0, 'c': 0, 's': 0}
    nr = con.execute('SELECT COUNT(*) FROM reaccionantes WHERE pub_id=?', (pid,)).fetchone()[0]
    ns = con.execute('SELECT COUNT(*) FROM compartidores WHERE pub_id=?', (pid,)).fetchone()[0]
    nc = con.execute('SELECT COUNT(*) FROM comentarios WHERE pub_id=?', (pid,)).fetchone()[0]
    t_ant = len(p['texto'] or '')
    nt = (d.get('texto') or '').strip()
    if nt and len(nt) > t_ant:
        con.execute('UPDATE publicaciones SET texto=? WHERE id=?', (nt, pid))
    if nt and len(nt) >= t_ant and not d.get('truncado'):
        con.execute('UPDATE publicaciones SET texto_cortado=0 WHERE id=?', (pid,))
    nts = (d.get('texto_compartido') or '').strip()
    if nts and len(nts) > len(p['texto_compartido'] or ''):
        con.execute('UPDATE publicaciones SET texto_compartido=? WHERE id=?', (nts, pid))
    hist.append({'p': pasada, 'f': t0[:16], 'r': nr, 'c': nc, 's': ns, 't': max(t_ant, len(nt)),
                 'nuevos': (nr - prev.get('r', 0)) + (nc - prev.get('c', 0)) + (ns - prev.get('s', 0)) + (1 if len(nt) > t_ant else 0)})
    con.execute('UPDATE publicaciones SET detalle_pasada=?, detalle_hist=? WHERE id=?', (pasada, json.dumps(hist[-30:]), pid))


def cmd_detalle(a):
    """Pide quién reaccionó y quién compartió de publicaciones concretas."""
    con = db()
    ids = resolver_pubs(con, a.ids or [], a.ultimas or 5, solo_permalink=True)
    if a.ultimas and not any(not str(t).isdigit() for t in (a.ids or [])):
        q = "SELECT id FROM publicaciones WHERE permalink<>'' "
        par = []
        if a.de:
            f = buscar_fuente(con, a.de)
            q += 'AND fuente_id=? '
            par.append(f['id'])
        q += 'ORDER BY COALESCE(fecha_aprox, primera_vez) DESC, id DESC LIMIT ?'
        par.append(a.ultimas)
        ids += [r['id'] for r in con.execute(q, par).fetchall()]
    if not ids:
        sys.exit('Indica números de publicación (python fb.py detalle 60 61), páginas/grupos (python fb.py detalle P1 P2 P3 -n 3) o --ultimas 10 [--de FUENTE].')
    items = []
    antes_det = {}
    hoy_ = datetime.now().strftime('%Y-%m-%d')
    for i in dict.fromkeys(ids):
        p = con.execute('SELECT * FROM publicaciones WHERE id=?', (i,)).fetchone()   # (completo: edad_hash necesita fecha y texto)
        est = estado_detalle(con, i) if p else None
        if getattr(a, 'reiniciar', False) and est:
            con.execute('UPDATE publicaciones SET detalle_pasada=0, detalle_hist=NULL WHERE id=?', (i,))
            con.commit()
            est = estado_detalle(con, i)
        if est and est['completo'] and not getattr(a, 'releer', False):
            print('#%s: su detalle ya está COMPLETO tras %d pasada(s): %d reacciones, %d comentarios, %d compartidos%s.' % (
                i, est['pasada'], est['nr'], est['nc'], est['ns'], ' (Facebook no da más nombres)' if est['estancado'] and not (est['r_ok'] and est['c_ok']) else ''))
            print('     Para leerlo otra vez de todos modos:  python3 fb.py detalle %s --releer     (o desde cero: --reiniciar)' % i)
            continue
        if not p:
            print('#%s: no existe.' % i)
        elif not p['permalink']:
            print('#%s: no tiene enlace propio guardado, no se puede abrir.' % i)
        elif not getattr(a, 'forzar', False) and url_reel(con, p):
            aviso_reel(i, url_reel(con, p))
        else:
            url_d = p['permalink'] if getattr(a, 'foto', False) else url_post_de_foto(con, p)
            if url_d != p['permalink']:
                print('#%s: su enlace es una foto; se abre la publicación: %s' % (p['id'], url_d))
            pe = con.execute('SELECT reacciones, ncompartidos FROM publicaciones WHERE id=?', (p['id'],)).fetchone()
            pasada_ = int(getattr(a, 'pasada', 0) or 0) or ((est['pasada'] + 1) if est else 1)
            hesp = 'fbv-esp=%d,%d' % (int(pe['reacciones'] or 0), int(pe['ncompartidos'] or 0)) + edad_hash(p) + '&fbv-pasada=%d' % pasada_
            antes_det[p['id']] = est
            if pasada_ > 1:
                print('#%d: PASADA %d (ya guardado: %d reacciones, %d comentarios, %d compartidos). Esta vez se espera y se lee más a fondo.' % (p['id'], pasada_, est['nr'], est['nc'], est['ns']))
            fu = con.execute('SELECT url, tipo FROM fuentes WHERE id=?', (p['fuente_id'],)).fetchone()
            pg = pagina_de_post_url(url_d)
            if not getattr(a, 'permalink', False) and fu and (fu['url'] or '') and fu['tipo'] in ('pagina', 'grupo', 'persona_grupo') and not a.foto:
                # POR DEFECTO: se abre la página/grupo y la extensión baja hasta la tarjeta de la publicación (ahí Facebook dibuja mejor las reacciones);
                # si no la encuentra, abre la dirección de la publicación.
                u_det = fu['url'].split('#')[0] + '#fbv-post=%s&fbv-id=%s&fbv-perma=%s&%s' % (quote(p['clave'] or '', safe=''), quote((pg or {}).get('id', ''), safe=''), quote(url_d.split('#')[0], safe=''), hesp)
            else:
                u_det = url_d.split('#')[0] + '#' + hesp
            items.append((u_det, 'detalle', p['id'], 0, '#%d %s' % (p['id'], p['autor'] or '')))
    if not items:
        return
    if not a.cola:
        # por defecto: todo en uno (abre Firefox, espera, y MUESTRA el resultado)
        con.close()
        asegurar_servidor()
        res = lanzar_items(items, paquete_navegador(a), a.espera, a.pausa, volver=True)
        con = db()
        OPC['reacciones'] = True
        OPC['enlaces'] = {1, 2, 3}   # enlaces: quien publica, quienes comentan, quienes reaccionan/comparten
        for (url, accion, pid, sc, nombre), r in zip(items, res):
            print('\n' + '=' * ancho())
            if r[1] != 'hecha':
                print('#%d: NO se pudo leer el detalle (%s). Mira: python fb.py cola' % (pid, r[2]))
            p = con.execute('SELECT * FROM publicaciones WHERE id=?', (pid,)).fetchone()
            nr = con.execute('SELECT COUNT(*) FROM reaccionantes WHERE pub_id=?', (pid,)).fetchone()[0]
            ns = con.execute('SELECT COUNT(*) FROM compartidores WHERE pub_id=?', (pid,)).fetchone()[0]
            print('Detalle de la publicación #%d: %d personas que reaccionaron, %d que compartieron.' % (pid, nr, ns))
            if r[1] == 'hecha' and r[2] and ('sin botón' in r[2] or 'no se abrió' in r[2]):
                print('Aviso de la extensión: %s' % r[2])
            if nr == 0 and ns == 0 and r[1] == 'hecha' and (p['detalle_limitado'] if 'detalle_limitado' in p.keys() else None):
                print('Facebook solo enseña los nombres de tus amigos que reaccionaron a esta publicación, y ninguno lo hizo (límite de Facebook en')
                print('páginas con muchas reacciones). No es un fallo: no hay más nombres que leer.')
            elif nr == 0 and ns == 0 and r[1] == 'hecha':
                dc = dir_diag_compartida()
                print('No se obtuvo ninguna persona. Se guardó un informe (.txt y el .html de la publicación) en:\n   %s\nMándamelos.' % (dc or ruta('diagnosticos')))
            mostrar_post(con, p, marcar=True)
            e0, e1 = antes_det.get(pid), estado_detalle(con, pid)
            if e0 and e1:
                print('\nPROGRESO DEL DETALLE de #%d (pasada %d):' % (pid, e1['pasada']))
                print(tabla(['', 'Antes', 'Ahora', 'Facebook indica', 'Estado'], [
                    ['Reacciones (personas)', e0['nr'], e1['nr'], e1['er'] or '-', 'completo' if e1['r_ok'] else 'faltan'],
                    ['Comentarios leídos', e0['nc'], e1['nc'], e1['ec'] or '-', 'completo' if e1['c_ok'] else 'faltan'],
                    ['Compartidos (personas)', e0['ns'], e1['ns'], e1['esp_s'] or '-', '-'],
                    ['Texto', 'cortado' if not e0['t_ok'] else 'entero', 'cortado' if not e1['t_ok'] else 'entero', '-', 'completo' if e1['t_ok'] else 'falta']]))
                if e1['completo']:
                    print('Detalle COMPLETO' + (' (dos pasadas seguidas sin novedades: Facebook no da más)' if e1['estancado'] and not (e1['r_ok'] and e1['c_ok']) else '') + '.')
                else:
                    print('Aún falta información. Vuelve a ejecutar:  python3 fb.py detalle %d     (la pasada %d espera más y lee más a fondo; lo ya guardado se conserva)' % (pid, e1['pasada'] + 1))
        con.commit()
        return
    for url, accion, pid, sc, nombre in items:
        i = encolar(con, url, 0, 'manual', 'detalle', pid)
        print('%s %s' % ('En cola:   ' if i else 'Ya estaba:', nombre))
    con.commit()
    print('\nSOLO se ha puesto en la cola (con --cola no se lee nada todavía). Cuando la extensión lo procese')
    print('(Firefox abierto, "Atender la cola" marcado, hasta 1 minuto de espera), mira el resultado con:')
    print('   python fb.py post Nº -r -e 3')


def cmd_pub(a):
    """Lee UNA publicación concreta por su URL: la asocia a su página/grupo, lee con calma sus comentarios y luego quién reaccionó y compartió."""
    con = db()
    pkg = paquete_navegador(a)
    asegurar_servidor()
    hoy_ = datetime.now().strftime('%Y-%m-%d')
    leidas_ = []
    for k, u in enumerate(a.urls, 1):
        u = u.strip()
        if RE_REEL.search(u) and not getattr(a, 'forzar', False):
            aviso_reel(None, u)
            continue
        pg = pagina_de_post_url(u)
        libre = False
        if not pg:
            if not re.search(r'^https?://([a-z0-9-]+\.)?(facebook\.com|fb\.com|fb\.watch)/', u):
                print('[%d/%d] Esto no es una dirección de Facebook: %s' % (k, len(a.urls), u))
                continue
            libre = True   # enlace corto (/share/p/...), foto, vídeo...: se abre igual y la página/grupo se deduce de quien la publica
            pg = {'clave': '', 'tipo': 'pagina', 'url': '', 'id': ''}
        t_ini = ahora()
        print('=' * ancho())
        if not libre:
            fid = fuente_de_pagina(con, pg)
            con.commit()
            f = con.execute('SELECT * FROM fuentes WHERE id=?', (fid,)).fetchone()
            print('[%d/%d] Publicación %s  ->  %s (%s)' % (k, len(a.urls), pg['id'][:24], f['nombre'], pg['url']))
            nom_ = f['nombre']
        else:
            print('[%d/%d] Publicación de dirección no estándar; la asociaré a la página de quien la publicó: %s' % (k, len(a.urls), u[:90]))
            nom_ = 'publicación'
        res = lanzar_items([(u.split('#')[0], 'capturar', None, 6, nom_)], pkg, a.espera, a.pausa, volver=False)
        if res[0][1] != 'hecha':
            print('NO se pudo leer la publicación (%s). Mira: python3 fb.py cola' % res[0][2])
            continue
        con.close()
        con = db()
        if libre:
            cap_ = con.execute('SELECT id, fuente_id FROM capturas WHERE cola_id=? ORDER BY id DESC LIMIT 1', (res[0][3],)).fetchone()
            p = con.execute('SELECT * FROM publicaciones WHERE captura_id=? ORDER BY orden, id LIMIT 1', (cap_['id'],)).fetchone() if cap_ else None
            if not p:
                print('La extensión no devolvió ninguna publicación (¿no cargó?). Mira: python3 fb.py diagnostico')
                continue
            ck_ = clave_de_url(p['autor_url'] or '')
            if ck_:
                pg = {'clave': ck_, 'tipo': 'grupo' if ck_.startswith('grupo-') else 'pagina', 'url': (p['autor_url'] or '').split('?')[0] if not (p['autor_url'] or '').count('profile.php') else p['autor_url'], 'id': ''}
                fid = fuente_de_pagina(con, pg)
                if fid != p['fuente_id']:
                    dup = con.execute('SELECT id FROM publicaciones WHERE fuente_id=? AND clave=?', (fid, p['clave'])).fetchone()
                    if dup:
                        con.execute('DELETE FROM publicaciones WHERE id=?', (p['id'],))
                        p = con.execute('SELECT * FROM publicaciones WHERE id=?', (dup['id'],)).fetchone()
                    else:
                        con.execute('UPDATE publicaciones SET fuente_id=? WHERE id=?', (fid, p['id']))
                    if not con.execute('SELECT 1 FROM publicaciones WHERE fuente_id=?', (cap_['fuente_id'],)).fetchone():
                        con.execute("DELETE FROM fuentes WHERE id=? AND tipo IN ('publicacion','otro','foto') AND vigilada=0", (cap_['fuente_id'],))
                    con.commit()
                    print('Asociada a la página/grupo de quien la publicó: %s' % ck_)
            else:
                fid = p['fuente_id']
                print('No pude deducir la página de quien la publicó (sin enlace de autor); queda en su fuente provisional.')
            f = con.execute('SELECT * FROM fuentes WHERE id=?', (fid,)).fetchone()
            pg['url'] = pg['url'] or (f['url'] or '')
            pg['id'] = (re.search(r'/(?:posts|videos|reel)/(\w+)', p['permalink'] or '') or [None, ''])[1] or ''
            idn = pg['id']
        else:
            idn = pg['id']
            p = con.execute("SELECT * FROM publicaciones WHERE fuente_id=? AND (clave LIKE ? OR permalink LIKE ?) ORDER BY ultima_vez DESC, id DESC LIMIT 1",
                            (fid, '%' + idn + '%', '%' + idn + '%')).fetchone()
        if not p:
            p = con.execute("SELECT * FROM publicaciones WHERE fuente_id=? AND ultima_vez>=? ORDER BY orden, id LIMIT 1", (fid, t_ini)).fetchone()
        if not p:
            print('La extensión no devolvió ninguna publicación (¿no cargó?). Mira: python3 fb.py diagnostico')
            continue
        f = con.execute('SELECT * FROM fuentes WHERE id=?', (fid,)).fetchone()
        if f['nombre'] == f['clave'] and (p['autor'] or '').strip():
            con.execute('UPDATE fuentes SET nombre=? WHERE id=?', (p['autor'].strip(), fid))
            f = con.execute('SELECT * FROM fuentes WHERE id=?', (fid,)).fetchone()
        con.commit()
        print('Guardada como #%d en %s (fecha: %s)' % (p['id'], f['nombre'], p['fecha_aprox'] or 'sin fecha legible'))
        if not a.sin_detalle:
            if (p['detalle_vez'] or '')[:10] == hoy_ and not a.releer:
                print('Su detalle (quién reaccionó/compartió) ya se leyó hoy; no se repite. Para forzarlo: --releer')
            else:
                con.close()
                hesp_ = 'fbv-esp=%d,%d' % (int(p['reacciones'] or 0), int(p['ncompartidos'] or 0)) + edad_hash(p)
                if getattr(a, 'en_pagina', False):
                    u_det = pg['url'] + '#fbv-post=%s&fbv-id=%s&fbv-perma=%s&%s' % (quote(p['clave'] or '', safe=''), quote(pg['id'], safe=''), quote(u.split('#')[0], safe=''), hesp_)
                else:
                    u_det = u.split('#')[0] + '#' + hesp_
                lanzar_items([(u_det, 'detalle', p['id'], 0, '#%d %s' % (p['id'], p['autor'] or ''))], pkg, max(a.espera, 300), a.pausa, volver=False)
                con = db()
        p = con.execute('SELECT * FROM publicaciones WHERE id=?', (p['id'],)).fetchone()
        OPC['reacciones'] = True
        OPC['enlaces'] = {1, 2, 3}
        mostrar_post(con, p, marcar=True)
        con.commit()
        leidas_.append(p['id'])
    volver_a_termux()
    return leidas_


# ----------------------------------------------------------------------------
# buscar EN Facebook (el cuadro "Buscar en Facebook"), elegir resultados y leerlos
# ----------------------------------------------------------------------------

SEG_BUSQUEDA = {'todo': 'top', 'todos': 'top', 'publicaciones': 'posts', 'posts': 'posts', 'personas': 'people', 'gente': 'people',
                'paginas': 'pages', 'páginas': 'pages', 'grupos': 'groups', 'videos': 'videos', 'vídeos': 'videos',
                'fotos': 'photos', 'eventos': 'events', 'lugares': 'places', 'lugar': 'places'}


def consulta_de(terminos, exacta=False):
    """Cada argumento con espacios = frase exacta (entre comillas); argumentos sueltos = palabras."""
    partes = []
    for t in terminos:
        t = t.strip().strip('"').strip()
        if not t:
            continue
        partes.append('"%s"' % t if (' ' in t or exacta) else t)
    return ' '.join(partes)


def url_busqueda(consulta, tipo):
    from urllib.parse import quote_plus
    return 'https://www.facebook.com/search/%s/?q=%s' % (SEG_BUSQUEDA[tipo], quote_plus(consulta))


def parse_seleccion(txt, maximo):
    t = (txt or '').strip().lower()
    if t in ('todos', 'todas', 'todo', 'all', 'a', '*'):
        return list(range(1, maximo + 1))
    sel = []
    for x in re.split(r'[,\s]+', t):
        if not x:
            continue
        m = re.match(r'^(\d+)-(\d+)$', x)
        if m:
            sel += list(range(int(m.group(1)), int(m.group(2)) + 1))
        elif x.isdigit():
            sel.append(int(x))
        else:
            sys.exit('Selección no válida: "%s". Usa por ejemplo: 1   1,3,5   2-4   todos' % x)
    return [n for n in dict.fromkeys(sel) if 1 <= n <= maximo]


def mostrar_resultados(con, bid):
    b = con.execute('SELECT * FROM busquedas WHERE id=?', (bid,)).fetchone()
    rs = con.execute('SELECT * FROM resultados WHERE busq_id=? ORDER BY n', (bid,)).fetchall()
    print('=' * ancho())
    print('BÚSQUEDA #%d  %s  [%s]  %s' % (b['id'], b['consulta'], b['tipo'], b['creada'][:16]))
    print('=' * ancho())
    if not rs:
        print('Sin resultados leídos.')
        print('Si la red sí mostró resultados: python fb.py diagnostico (o x.py diagnostico) y mándame el informe o la página guardada con SingleFile.')
        return rs
    for r in rs:
        print('%2d. %s%s  [%s]' % (r['n'], '(leído) ' if r['leido'] else '', r['titulo'] or '(sin título)', r['tipo'] or '?'))
        if r['detalle']:
            print(textwrap.fill(r['detalle'], width=ancho(), initial_indent='      ', subsequent_indent='      '))
        if r['url']:
            print('      ' + r['url'][:150])
    print()
    return rs


def leer_resultados(con, bid, sel, a):
    """Abre en Firefox los resultados elegidos, los captura y muestra su contenido."""
    rs = {r['n']: r for r in con.execute('SELECT * FROM resultados WHERE busq_id=?', (bid,)).fetchall()}
    items = []
    for n in sel:
        r = rs.get(n)
        if not r or not r['url']:
            print('%s: sin enlace, no se puede abrir.' % n)
            continue
        items.append((r['url'], 'capturar', None, a.scroll if getattr(a, 'scroll', None) is not None else int(config()['scrolls']),
                      '%d. %s' % (n, (r['titulo'] or '')[:50])))
    if not items:
        return
    con.close()
    asegurar_servidor()
    res = lanzar_items(items, paquete_navegador(a), a.espera, a.pausa, volver=True)
    con = db()
    print()
    for (url, _, _, _, nombre), (_, estado, det, cid) in zip(items, res):
        print('#' * ancho())
        print(nombre)
        print('#' * ancho())
        if estado != 'hecha':
            print('  No se pudo leer (%s). Reintenta: python fb.py elegir %s -b %d' % (det or estado, nombre.split('.')[0], bid))
            print()
            continue
        c = con.execute('SELECT id, fuente_id FROM capturas WHERE cola_id=? ORDER BY id DESC LIMIT 1', (cid,)).fetchone()
        if not c:
            print('  (la captura no quedó registrada)')
            continue
        f = con.execute('SELECT * FROM fuentes WHERE id=?', (c['fuente_id'],)).fetchone()
        n_ = int(nombre.split('.')[0])
        con.execute('UPDATE resultados SET leido=1 WHERE busq_id=? AND n=?', (bid, n_))
        ps = posts_de(con, f['id'], False, 1 if f['tipo'] == 'publicacion' else a.n)
        if not ps:
            print('  0 publicaciones leídas en %s (puede ser un perfil privado o una página sin publicaciones visibles).' % f['nombre'])
        for p in ps:
            mostrar_post(con, p, marcar=not a.no_marcar)
        con.commit()
    con.commit()


def cmd_fbuscar(a):
    """Busca en Facebook (como el cuadro de arriba a la izquierda), muestra los resultados y deja elegir."""
    con = db()
    consulta = consulta_de(a.consulta, a.exacta)
    if not consulta:
        sys.exit('Escribe qué buscar.  Frase:  python fb.py fbuscar "una frase"     Palabras:  python fb.py fbuscar palabra otra')
    tipo = a.tipo.lower()
    if tipo not in SEG_BUSQUEDA:
        sys.exit('Tipo no válido. Usa: ' + ', '.join(sorted(set(SEG_BUSQUEDA))))
    url = url_busqueda(consulta, tipo)
    cur = con.execute('INSERT INTO busquedas(consulta,tipo,url,creada) VALUES(?,?,?,?)', (consulta, tipo, url, ahora()))
    bid = cur.lastrowid
    con.commit()
    sc = a.scroll if a.scroll is not None else 4
    if a.sin_abrir:
        encolar(con, url, sc, 'manual', 'buscar', bid)
        con.commit()
        print('Búsqueda #%d en la cola. La extensión la hará (con Firefox abierto). Luego: python fb.py resultados %d' % (bid, bid))
        return
    pkg = paquete_navegador(a)
    print('Buscando en Facebook: %s   [%s]' % (consulta, tipo))
    con.close()
    asegurar_servidor()
    res = lanzar_items([(url, 'buscar', bid, sc, consulta)], pkg, a.espera, 0, volver=True)
    con = db()
    if res[0][1] != 'hecha':
        sys.exit('La búsqueda no terminó (%s).  Mira: python fb.py cola' % (res[0][2] or res[0][1]))
    rs = mostrar_resultados(con, bid)
    if not rs:
        return
    sel_txt = a.elegir
    if sel_txt is None and sys.stdin.isatty() and not a.no_preguntar:
        try:
            sel_txt = input('¿Cuáles quieres leer? (1   1,3,5   2-4   todos   Intro = ninguno): ')
        except EOFError:
            sel_txt = ''
    if sel_txt:
        leer_resultados(con, bid, parse_seleccion(sel_txt, len(rs)), a)
    else:
        print('Para leer alguno más tarde:  python fb.py elegir 1,3 -b %d' % bid)


def cmd_resultados(a):
    con = db()
    if a.busqueda:
        bid = a.busqueda
    else:
        r = con.execute('SELECT MAX(id) FROM busquedas').fetchone()
        bid = r[0]
    if not bid:
        sys.exit('Aún no has hecho ninguna búsqueda:  python fb.py fbuscar "texto"')
    if not con.execute('SELECT 1 FROM busquedas WHERE id=?', (bid,)).fetchone():
        sys.exit('No existe la búsqueda %s.' % bid)
    if a.listar:
        print(tabla(['Nº', 'Fecha', 'Tipo', 'Resultados', 'Consulta'], [[b['id'], b['creada'][:16], b['tipo'], b['n_resultados'], b['consulta']]
                                                                      for b in con.execute('SELECT * FROM busquedas ORDER BY id DESC LIMIT 30').fetchall()]))
        return
    mostrar_resultados(con, bid)


def cmd_elegir(a):
    con = db()
    bid = a.busqueda or con.execute('SELECT MAX(id) FROM busquedas').fetchone()[0]
    if not bid:
        sys.exit('Aún no has hecho ninguna búsqueda.')
    n = con.execute('SELECT COUNT(*) FROM resultados WHERE busq_id=?', (bid,)).fetchone()[0]
    sel = parse_seleccion(a.seleccion, n)
    if not sel:
        sys.exit('Ningún número válido (la búsqueda #%d tiene %d resultados).' % (bid, n))
    leer_resultados(con, bid, sel, a)



def cmd_cola(a):
    con = db()
    if a.accion == 'limpiar':
        n = con.execute("DELETE FROM cola WHERE estado IN ('hecha','error')").rowcount
        con.commit()
        print('Eliminadas %d entradas terminadas.' % n)
        return
    if a.accion == 'reintentar':
        n = con.execute("UPDATE cola SET estado='pendiente', actualizada=? WHERE estado IN ('error','abriendo','en_curso')", (ahora(),)).rowcount
        con.commit()
        print('%d entradas vuelven a pendiente.' % n)
        return
    if a.accion == 'vaciar':
        n = con.execute('DELETE FROM cola').rowcount
        con.commit()
        print('Cola vaciada (%d).' % n)
        return
    filas = con.execute('SELECT * FROM cola ORDER BY id DESC LIMIT 40').fetchall()
    if not filas:
        print('La cola está vacía.')
        return
    print(tabla(['Nº', 'Estado', 'Creada', 'URL', 'Detalle'], [[r['id'], r['estado'], r['creada'][5:16],
                                                                  ('' if (r['accion'] or 'capturar') == 'capturar' else '[%s #%s] ' % (r['accion'], r['pub_id'] or '')) + (r['url'] or '')[:60],
                                                                  r['detalle'] or ''] for r in filas]))


def cmd_sugerir(a):
    """Compara contactos, páginas, personas que sigo, amigos y grupos con las fuentes vigiladas y sugiere añadir los que faltan."""
    con = db()
    orden = [('contacto', 'Contactos'), ('siguiendo', 'Personas/páginas que sigo'), ('pagina', 'Páginas'),
             ('grupo', 'Grupos'), ('amigo', 'Amigos')]
    if a.de:
        cat = {x.strip().lower() for x in a.de.split(',') if x.strip()}
        orden = [(t, r) for t, r in orden if CAT_LISTA[t] in cat]
    avisos = []
    resumen = []
    faltan = {}   # destino normalizado -> dict
    for tipo, rotulo in orden:
        filas = con.execute("SELECT * FROM listas WHERE tipo=? AND baja IS NULL ORDER BY nombre", (tipo,)).fetchall()
        if not filas:
            resumen.append([rotulo, 0, 0, 0, 'NINGUNO guardado'])
            if tipo == 'contacto':
                avisos.append('No tienes ningún contacto guardado: la columna de contactos de la página principal no se ha capturado '
                              '(o está vacía). Prueba: python fb.py lanzar --listas   (o: python fb.py actualizar --solo inicio)')
            elif tipo == 'amigo':
                avisos.append('No hay lista de amigos guardada (o no tienes amigos visibles). Captúrala con: python fb.py actualizar --solo amigos')
            else:
                avisos.append('La lista "%s" está vacía; captúrala con: python fb.py actualizar --solo %s' %
                              (rotulo, {'siguiendo': 'personas', 'pagina': 'paginas', 'grupo': 'grupos'}.get(tipo, 'inicio')))
            continue
        vig = sinurl = 0
        for r in filas:
            u = url_de_lista(r)
            if not u:
                sinurl += 1
                continue
            f = fuente_de_lista(con, r)
            if f and f['vigilada']:
                vig += 1
                continue
            if f and f['bloqueada']:
                continue   # la desvigilaste tú: no se vuelve a sugerir
            k = ('f%d' % f['id']) if f else norm_url(u)
            e = faltan.setdefault(k, {'nombre': r['nombre'], 'url': u, 'f': f, 'refs': [], 'cats': []})
            e['refs'].append('%s%d' % (TIPO_LETRA.get(tipo, '?'), r['id']))
            e['cats'].append(CAT_LISTA[tipo])
        resumen.append([rotulo, len(filas), vig, len(filas) - vig - sinurl, ('%d sin enlace' % sinurl) if sinurl else ''])
    print('REVISIÓN DE LO QUE SIGUES FRENTE A LO QUE VIGILAS\n')
    print(tabla(['Lista', 'Elementos', 'Vigilados', 'Sin vigilar', 'Nota'], resumen))
    for t in avisos:
        print('\n(!) ' + t)
    if not faltan:
        print('\nTodo lo que sigues y tienes en tus listas ya está en la lista de vigilados.')
        return
    items = list(faltan.values())
    total = len(items)
    if a.max and len(items) > a.max:
        items = items[:a.max]
    print('\nSUGERENCIAS: %d sin vigilar%s\n' % (total, (' (se muestran %d; usa --max para más)' % len(items)) if len(items) < total else ''))
    filas = []
    for e in items:
        filas.append([e['refs'][0], ','.join(dict.fromkeys(e['cats'])), e['nombre'] or '?',
                      ('ya capturada, sin vigilar' if e['f'] else 'sin capturar'), e['url'][:60]])
    print(tabla_c(['Ref', 'Está en', 'Nombre', 'Estado', 'URL'], filas))
    if a.anadir:
        n = c = 0
        sc = a.scroll if a.scroll is not None else int(config()['scrolls'])
        for e in items:
            if e['f']:
                con.execute('UPDATE fuentes SET vigilada=1 WHERE id=?', (e['f']['id'],))
                n += 1
            else:
                registrar_vigilada(con, e['url'], e['nombre'])
                if encolar(con, e['url'], sc, 'manual', vigilar=1):
                    c += 1
        con.commit()
        print('\nHecho: %d marcadas como vigiladas, %d puestas en la cola para capturarlas (al capturarse quedan vigiladas).' % (n, c))
        if c:
            print('La extensión las irá visitando; seguimiento: python fb.py cola')
    else:
        print('\nPara añadirlas TODAS a la vigilancia:   python fb.py sugerir --anadir')
        print('Solo algunas:                            python fb.py vigilar %s' % ' '.join(e['refs'][0] for e in items[:3]))
        print('Solo una categoría:                      python fb.py sugerir --de paginas --anadir   (contactos, personas, paginas, grupos, amigos)')


def cmd_desvigilar(a):
    """Deja de vigilar (o vuelve a vigilar con --activar) una o varias fuentes, junto con sus duplicadas."""
    con = db()
    for q in a.fuente:
        f = buscar_fuente(con, q)
        ids, canc = poner_vigilada(con, f, 1 if a.activar else 0)
        con.commit()
        print('%s: vigilada = %s' % (f['nombre'], 'sí' if a.activar else 'NO'))
        extra = [con.execute('SELECT id, nombre FROM fuentes WHERE id=?', (i,)).fetchone() for i in ids if i != f['id']]
        if extra:
            print('   (también la misma página/grupo guardada como: %s)' % ', '.join('#%d %s' % (e['id'], e['nombre']) for e in extra))
        if canc:
            print('   %d orden(es) pendientes de la cola anuladas.' % canc)
        if not a.activar:
            print('   No se volverá a vigilar sola, aunque aparezca de nuevo en tus listas. Para volver: python fb.py desvigilar %d --activar' % f['id'])
    # órdenes viejas que quedaran en la cola de otras fuentes desvigiladas
    n = limpiar_cola_obsoleta(con)
    con.commit()
    if n:
        print('Anuladas además %d órdenes antiguas de la cola de fuentes desvigiladas.' % n)


def limpiar_cola_obsoleta(con):
    ids = [r['id'] for r in con.execute('SELECT id FROM fuentes WHERE bloqueada=1').fetchall()]
    return cancelar_cola_de(con, ids) if ids else 0


def cmd_duplicadas(a):
    """Lista las fuentes que son la misma página/grupo guardada varias veces; con --unir las junta."""
    con = db()
    vistos = set()
    grupos = []
    for f in con.execute("SELECT * FROM fuentes WHERE tipo IN ('pagina','grupo') ORDER BY id").fetchall():
        if f['id'] in vistos:
            continue
        ids = misma_fuente(con, f)
        if len(ids) > 1:
            vistos |= ids
            grupos.append(sorted(ids))
    if not grupos:
        print('No hay fuentes duplicadas.')
        return
    filas = []
    for k, g in enumerate(grupos, 1):
        for i in g:
            f = con.execute('SELECT * FROM fuentes WHERE id=?', (i,)).fetchone()
            npub = con.execute('SELECT COUNT(*) FROM publicaciones WHERE fuente_id=?', (i,)).fetchone()[0]
            filas.append([k, f['id'], f['tipo'], f['nombre'], f['clave'], npub, 'sí' if f['vigilada'] else 'no', 'sí' if f['bloqueada'] else ''])
    print(tabla_c(['Grupo', 'Nº', 'Tipo', 'Nombre', 'Clave', 'Public.', 'Vigilada', 'Desvigilada por ti'], filas))
    if not a.unir:
        print('\n%d página(s)/grupo(s) guardados más de una vez. Para juntar cada grupo en su fuente más antigua:' % len(grupos))
        print('   python fb.py duplicadas --unir        (o una sola pareja: python fb.py unir 12 5   -> mete la 5 en la 12)')
        return
    for g in grupos:
        base = con.execute('SELECT * FROM fuentes WHERE id=?', (g[0],)).fetchone()
        for i in g[1:]:
            o = con.execute('SELECT * FROM fuentes WHERE id=?', (i,)).fetchone()
            if o:
                unir_fuentes(con, base, o)
                base = con.execute('SELECT * FROM fuentes WHERE id=?', (g[0],)).fetchone()
    con.commit()
    print('\nUnidas. Comprueba con: python fb.py fuentes')


def cmd_unir(a):
    con = db()
    d = buscar_fuente(con, a.destino)
    o = buscar_fuente(con, a.origen)
    if d['id'] == o['id']:
        sys.exit('Son la misma fuente.')
    unir_fuentes(con, d, o)
    con.commit()
    print('Unida #%d "%s" dentro de #%d "%s".' % (o['id'], o['nombre'], d['id'], d['nombre']))


def cmd_marcar(a):
    con = db()
    if a.fuente:
        f = buscar_fuente(con, a.fuente)
        con.execute('UPDATE publicaciones SET visto=1 WHERE fuente_id=?', (f['id'],))
        con.execute('UPDATE comentarios SET visto=1 WHERE pub_id IN (SELECT id FROM publicaciones WHERE fuente_id=?)', (f['id'],))
    else:
        con.execute('UPDATE publicaciones SET visto=1')
        con.execute('UPDATE comentarios SET visto=1')
        con.execute('UPDATE listas SET visto=1')
        con.execute('UPDATE mensajes SET visto=1')
    con.commit()
    print('Marcado como visto.')


def cmd_exportar(a):
    con = db()
    f = buscar_fuente(con, a.fuente)
    arch = a.salida or ruta('exportaciones', '%s_%s.txt' % (slug(f['nombre']), datetime.now().strftime('%Y%m%d_%H%M')))
    import io
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        print('FB Vigía - exportación')
        print('Fuente: %s (%s)' % (f['nombre'], f['tipo']))
        print('URL: %s' % f['url'])
        print('Generado: %s\n' % ahora())
        for p in posts_de(con, f['id'], False, 0):
            mostrar_post(con, p, marcar=False)
    finally:
        sys.stdout = old
    with open(arch, 'w', encoding='utf-8') as fh:
        fh.write(buf.getvalue())
    print('Exportado a: %s' % arch)


def cmd_estado(a):
    con = db()
    c = config()
    print('FB Vigía %s' % VERSION)
    print('Carpeta de datos: %s' % DIR)
    for t, n in (('fuentes', 'Fuentes'), ('publicaciones', 'Publicaciones'), ('comentarios', 'Comentarios'),
                 ('imagenes', 'Imágenes'), ('enlaces', 'Enlaces'), ('listas', 'Elementos de listas'),
                 ('mensajes', 'Mensajes'), ('cola', 'Cola')):
        print('  %-20s %d' % (n + ':', con.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]))
    try:
        r = urllib.request.urlopen('http://127.0.0.1:%d/ping' % int(c['puerto']), timeout=2).read()
        print('Servidor: ACTIVO en el puerto %d' % int(c['puerto']))
    except Exception:
        print('Servidor: apagado  (arráncalo con: python fb.py servidor)')


# ----------------------------------------------------------------------------
# ayuda y programa principal
# ----------------------------------------------------------------------------

AYUDA = """FB Vigía %s - vigilar Facebook desde Termux

PREPARACIÓN (una vez por sesión de Termux)
  python fb.py servidor            Arranca el receptor. Déjalo abierto (o: nohup python fb.py servidor &).

VIGILAR
  python fb.py vigilar URL|P3|G2... [--sin-capturar]   Las apunta en VIGILADOS y las lee ya.
  python fb.py vigilados [--solo 5 8] [--ninguna]      Ver/cambiar la lista que abre "lanzar".
  python fb.py cantidad [P3=30 G2=10 12=5] [--todas N]  Cuántas publicaciones nuevas se leen de CADA fuente al lanzar (sin nada: tabla).
  python fb.py vigilar P3 | G2 | C5                          Por referencia de lista (ver "listas").
  python fb.py vigilar --lista paginas [--max 20]            Todas las de una lista.
  python fb.py vigilar --todas                               Repite todas las fuentes vigiladas.
  python fb.py vigilar --inicio                              Captura la página principal (contactos, grupos...).
  python fb.py cola [limpiar|reintentar|vaciar]              Ver / gestionar la cola.
     (también puedes capturar a mano desde el panel de la extensión)

  python fb.py sugerir [--de paginas,personas,...] [--anadir]   Compara lo que sigues con lo vigilado y sugiere añadir.

TODO DESDE TERMUX, SIN TOCAR FIREFOX
  python fb.py lanzar                          Abre en Firefox TODAS las páginas vigiladas, una a una; la extensión
                                               las captura sola y cierra cada pestaña. Al final, resumen de novedades.
  python fb.py lanzar P3 G2 URL [--de grupos] [--listas] [--max N] [--espera 240] [--navegador PAQUETE]
  python fb.py lanzar P141 -n 50               Esa página, 50 publicaciones (solo esta vez). También: P141:50 77:20
  python fb.py vigilar P141 --sin-capturar -n 50 --recordar   Fijar 50 para esa página
  python fb.py abiertas                        Captura las pestañas de Facebook que ya tengas abiertas.
  python fb.py pub URL [URL...] [--sin-detalle]      Lee UNA publicación por su dirección (aunque sea antigua): la guarda en su
                                              página o grupo (p. ej. Diario de Ávila), lee con calma comentarios y luego
                                              quién reaccionó y compartió, y la muestra.
  python fb.py detalle 60 61 | --ultimas 5 [--cola]   Lee (abre Firefox) y MUESTRA quién reaccionó y compartió.
  (repetirlo sobre la misma publicación = otra PASADA, más a fondo, que suma a lo guardado; --reiniciar / --releer / --pasada K)

ALIMENTAR PUBLICACIONES DE UNA FECHA O INTERVALO (páginas y grupos)
  python fb.py alimentar P289 --dia 2026-10-01             lee Diario de Ávila hasta llegar a ese día y lo guarda
  python fb.py alimentar P289 G2 --desde 1/10 --hasta 5/10    varias fuentes, un intervalo
  python fb.py alimentar --ultimos-dias 7 --de paginas,grupos    todas las vigiladas de esas categorías
     No muestra las publicaciones: las guarda y da un resumen. Para verlas:  python fb.py dia P289 --dia 2026-10-01
     Si ya hay guardado hasta esa fecha no vuelve a leer (--forzar para releer). --max 300 sube el tope.

BUSCAR DENTRO DE UNA PÁGINA (su buscador propio) Y GUARDAR LO QUE SALE
  python fb.py buscaren P289 ciberseguridad -n 20      busca esa palabra en la página P289 y muestra lo encontrado
  python fb.py G1 habitacion -n 20 [--recientes] [--anio 2026]   busca dentro del grupo G1 (forma corta de buscaren)
  python fb.py buscaren P289 "https://www.facebook.com/profile/ID/search/?q=..."   una búsqueda ya filtrada (año, recientes,
     vistas): aplica el filtro en Firefox, copia la dirección y pégala entre comillas.

BUSCAR EN FACEBOOK (el cuadro "Buscar en Facebook") Y LEER LOS RESULTADOS
  python fb.py fbuscar "una frase exacta"      frase entre comillas
  python fb.py fbuscar palabra otra            varias palabras sueltas
  python fb.py fbuscar "ayuda vecinos" --tipo grupos     (todo, publicaciones, personas, paginas, grupos...)
     Muestra los resultados numerados y pregunta cuáles leer (1   1,3,5   2-4   todos).
  python fb.py resultados [Nº] | --listar      volver a ver una búsqueda
  python fb.py elegir 1,3 [-b Nº] [-n 5]       abrir y leer los resultados elegidos

ACTUALIZAR TODAS LAS LISTAS DE GOLPE (contactos, páginas, grupos, amigos, personas, mensajes, miembros)
  python fb.py actualizar [--solo paginas,grupos] [--fuentes] [--conversaciones N] [--sin-miembros]

VER LAS ÚLTIMAS PUBLICACIONES (por fecha, de una fuente o mezcladas)
  python fb.py ultimas P3 -n 5                 las 5 últimas de la página con referencia P3
  python fb.py ultimas -n 10                   las 10 últimas de TODO lo capturado (y mensajes)
  python fb.py ultimas -n 10 --de paginas,grupos,contactos,personas,mensajes
  Opciones de lectura (ver, nuevas, ultimas, post):
     -r / --reacciones      quién reaccionó (nombre y emoji) y quién compartió (tras "detalle")
     -e 1,2,3 / -e todos    enlaces a perfiles: 1=quien publica 2=quienes comentan 3=reaccionan/comparten
  Las imágenes salen con su ruta local (Img1: /ruta/...). Las referencias (P3, G2, C5, A7, S4, V1) salen en la columna Ref de: python fb.py listas ...

LEER (las novedades salen primero y quedan marcadas como vistas)
  python fb.py fuentes                      Lista de fuentes y cuántas novedades tiene cada una.
  python fb.py nuevas [--resumen]           Novedades de todas las fuentes.
  python fb.py ver FUENTE [-n 15] [--solo-nuevas] [--no-marcar] [--sin-comentarios]
  python fb.py post Nº                      Una publicación completa.
  python fb.py buscar "texto" [FUENTE]
  python fb.py exportar FUENTE [-o archivo.txt]

UN DÍA O VARIOS DÍAS, Y SEGUIR UN PUEBLO
  python fb.py dia 77 --dia ayer               Lo guardado de la fuente 77 de ayer (también: --dia 9/10 10/10, sabado, 2026-10-09)
  python fb.py dia P141 --desde 5/10 --hasta 9/10 --leer   Lee hacia atrás hasta el 5/10 y muestra esos días
  python fb.py dia --ultimos-dias 3            Lo guardado de TODAS las fuentes de los últimos 3 días
  python fb.py lugar "La Adrada" --pagina URL --busca "La Adrada"   Define y lee un pueblo (páginas, grupos, búsquedas)
  python fb.py lugar "La Adrada"               Actualizarlo      |   python fb.py lugares   Ver los guardados

MI ACTIVIDAD Y LO RECIBIDO
  python fb.py perfiles --anadir NÚMERO nombre   Apunta tu perfil o una página que administras
  python fb.py actividad nombre --leer           Lee tu Registro de actividad (cambia antes al perfil en Firefox)
  python fb.py actividad nombre --dia hoy        Lo que hiciste (--tipo comentario, --texto X, --resumen, --urls)
  python fb.py recibido nombre --leer [--detalle -r]   Comentarios y reacciones en tus publicaciones (--en-otras: en las ajenas)

CONTACTOS, PÁGINAS, PERSONAS, MENSAJES
  python fb.py listas                       Resumen de listas guardadas.
  python fb.py listas contactos|paginas|grupos|amigos|personas|miembros [--nuevos] [--ambito GRUPO]
  python fb.py mensajes [--nuevos] [CONVERSACIÓN]

IMÁGENES Y ENLACES
  python fb.py cortadas [--sospechosas] [--marcar]   Publicaciones con el texto cortado («Ver más» sin pulsar) y cómo releerlas.
  python fb.py ia [--probar IMAGEN] [--activar|--desactivar] [--ollama MODELO]   Estado del OCR y del modelo local; activar la descripción automática.
  python fb.py imgtexto P1 | 60 61 [-n 3] [--todas] | --pendientes   Texto (OCR) y qué se ve en las imágenes (--todas: todas las de cada publicación).
  python fb.py lanzar ... --describir       Al terminar, lee y describe TODAS las imágenes captadas.
  python fb.py lanzar ... --describir --fotogramas 2   ... y de cada vídeo/reel saca 2 fotogramas y los describe también.
  python fb.py copiar P1 | 60 61 [-n 10] [--a CARPETA]   Copia las imágenes/fotogramas de esas publicaciones a UNA carpeta.
  python fb.py original 60 | P1 -n 3        De una publicación que COMPARTE otra: lee la original (reacciones, comentarios, imágenes).
  python fb.py video 60 | P1 [--fotos 5] [--segundos 5]   Fotogramas de los primeros segundos de un vídeo.
  python fb.py repetidas [--aplicar]        Une publicaciones repetidas (misma publicación leída recortada y completa).
  python fb.py imagenes [FUENTE]            Descarga las pendientes y lista (carpeta imagenes/).
  python fb.py enlaces [FUENTE]             Enlaces de las publicaciones.
  python fb.py seguir FUENTE [--max 20]     Descarga los enlaces externos; los de Facebook van a la cola.

OTROS
  python fb.py diagnostico                  Último informe si una captura sale vacía.
  python fb.py estado | marcar [FUENTE] | desvigilar FUENTE [--activar]

FUENTE = número, nombre (o parte), clave o parte de la URL.   Datos en: %s
"""


def opciones_extra(p):
    p.add_argument('-e', '--enlaces', metavar='LISTA', nargs='?', const='todos',
                   help='mostrar enlaces a perfiles: 1=quien publica 2=quienes comentan 3=quienes reaccionan/comparten; ej. -e 1,2,3 o -e todos (solo -e = todos)')
    p.add_argument('-r', '--reacciones', action='store_true', help='mostrar quién reaccionó/compartió (si se consultó con "detalle")')


def opciones_dia(p):
    p.add_argument('--dia', nargs='+', help='uno o varios días concretos: 2026-10-09  9/10/2026  9/10  hoy  ayer  sabado')
    p.add_argument('--desde', help='primer día de un rango (hasta hoy si no pones --hasta)')
    p.add_argument('--hasta', help='último día del rango')
    p.add_argument('--ultimos-dias', type=int, help='los últimos N días, contando hoy')


_TERMINAL_PROTEGIDA = False


def proteger_terminal():
    """Los programas que fb.py lanza (am, termux-*, yt-dlp, tesseract...) pueden dejar la terminal de Termux sin eco (se escribe pero no se
    ven las letras). Se guarda el estado de la terminal al empezar y se restaura SIEMPRE al salir (también con Ctrl+C o error);
    y a los subprocesos no se les da la terminal como entrada (stdin) salvo que se pida."""
    global _TERMINAL_PROTEGIDA
    if _TERMINAL_PROTEGIDA:
        return
    _TERMINAL_PROTEGIDA = True
    try:
        import atexit
        import signal
        _orig = subprocess.Popen.__init__

        def _init(self, *args, **kw):
            if kw.get('stdin') is None and not kw.get('input'):
                kw['stdin'] = subprocess.DEVNULL
            _orig(self, *args, **kw)
        subprocess.Popen.__init__ = _init
        if not sys.stdin or not sys.stdin.isatty():
            return
        import termios
        fd = sys.stdin.fileno()
        guardado = termios.tcgetattr(fd)

        def restaurar():
            try:
                termios.tcsetattr(fd, termios.TCSANOW, guardado)
            except Exception:
                pass
            try:
                lista = termios.tcgetattr(fd)
                lista[3] |= (termios.ECHO | termios.ICANON)     # eco y edición de línea siempre activos
                termios.tcsetattr(fd, termios.TCSANOW, lista)
            except Exception:
                pass
            try:
                if os.environ.get('TERM'):
                    os.system('stty sane 2>/dev/null </dev/tty')
            except Exception:
                pass
        atexit.register(restaurar)

        def al_terminar(sig, frm):
            restaurar()
            sys.exit(128 + sig)
        for sg in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sg, al_terminar)
            except Exception:
                pass
    except Exception:
        pass


def main():
    proteger_terminal()
    if len(sys.argv) >= 3 and REF_RE.match(sys.argv[1]) and not sys.argv[2].startswith('-') and sys.argv[1][0].upper() in 'PG':
        sys.argv.insert(1, 'buscaren')      # python3 fb.py G1 palabra  ==  python3 fb.py buscaren G1 palabra
    if '--version' in sys.argv[1:] or '-V' in sys.argv[1:]:
        print('FB Vigía (fb.py) %s   %s' % (VERSION, os.path.abspath(__file__)))
        return
    ap = argparse.ArgumentParser(prog='fb.py', add_help=False)
    sp = ap.add_subparsers(dest='cmd')

    def nuevo(nombre, fn, **kw):
        p = sp.add_parser(nombre, **kw)
        p.set_defaults(fn=fn)
        return p

    nuevo('puertos', cmd_puertos)
    p = nuevo('puerto', cmd_puerto)
    p.add_argument('numero', nargs='?', type=int, help='nuevo puerto (sin número: muestra el actual y si choca con algo)')
    p.add_argument('--forzar', action='store_true', help='guardarlo aunque otro programa lo esté usando')
    p = nuevo('servidor', cmd_servidor)
    p.add_argument('--puerto', type=int)
    nuevo('fuentes', cmd_fuentes)
    p = nuevo('ver', cmd_ver)
    opciones_extra(p)
    p.add_argument('fuente')
    p.add_argument('-n', type=int, default=15)
    p.add_argument('--solo-nuevas', action='store_true')
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--sin-comentarios', action='store_true')
    p = nuevo('nuevas', cmd_nuevas)
    opciones_extra(p)
    p.add_argument('-n', type=int, default=0)
    p.add_argument('--resumen', action='store_true')
    p.add_argument('--no-marcar', action='store_true')
    p = nuevo('buscar', cmd_buscar)
    p.add_argument('texto')
    p.add_argument('fuente', nargs='?')
    p.add_argument('-n', type=int, default=50)
    p = nuevo('ultimas', cmd_ultimas)
    opciones_extra(p)
    p.add_argument('fuente', nargs='?', help='fuente o referencia (P3, G2...); sin ella, mezcla todo')
    p.add_argument('-n', type=int, default=10)
    p.add_argument('--de', help='filtrar: paginas,grupos,contactos,personas,amigos,inicio,mensajes')
    p.add_argument('--detalle', action='store_true', help='antes de mostrarlas, leer en Facebook quién reaccionó/compartió (abre Firefox)')
    p.add_argument('--releer', action='store_true', help='con --detalle: leer aunque ya se haya leído hoy')
    p.add_argument('--navegador')
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--sin-comentarios', action='store_true')
    p = nuevo('buscaren', cmd_buscaren)
    p.add_argument('fuente', help='página o grupo: referencia (P289, G1), nº o nombre; o directamente la URL de una búsqueda')
    p.add_argument('termino', nargs='?', help='palabra a buscar, o la URL de una búsqueda copiada del navegador (con filtros)')
    p.add_argument('-n', type=int, default=20, help='publicaciones de resultados a leer (20)')
    p.add_argument('--recientes', action='store_true', help='filtro «Más recientes»')
    p.add_argument('--anio', type=int, help='filtro de año de publicación (p. ej. 2026)')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--navegador')
    p.add_argument('--sin-volver', action='store_true')
    p = nuevo('alimentar', cmd_alimentar)
    opciones_dia(p)
    p.add_argument('fuentes', nargs='*', help='páginas/grupos: referencia (P141, G2), nº, nombre o URL; sin nada: todas las vigiladas')
    p.add_argument('--de', help='sin fuentes: solo las vigiladas de estas categorías (paginas,grupos,...)')
    p.add_argument('--forzar', action='store_true', help='(ya no hace falta: siempre se busca)')
    p.add_argument('--releer', action='store_true', help='leer a fondo también lo ya leído hoy (por defecto cada publicación se lee a fondo una vez al día)')
    p.add_argument('--empezar', type=int, default=20, help='publicaciones de la primera lectura (20); si no llega, se duplica')
    p.add_argument('--max', type=int, default=600, help='tope de publicaciones a recorrer (600)')
    p.add_argument('--fase', type=int, default=80, help='publicaciones por fase (80); cada fase se guarda y la siguiente retoma donde acabó')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    p = nuevo('dia', cmd_dia)
    opciones_dia(p)
    p.add_argument('fuente', nargs='?', help='fuente, referencia (P141), URL; sin ella: todo lo guardado')
    p.add_argument('--leer', action='store_true', help='antes de mostrar, abre la página y busca esos días (lee a fondo solo las publicaciones de esas fechas)')
    p.add_argument('--releer', action='store_true', help='con --leer: leer a fondo también lo ya leído hoy')
    p.add_argument('--empezar', type=int, default=20, help='con --leer: publicaciones de la primera lectura (20); si no llega, se duplica')
    p.add_argument('--max', type=int, default=600, help='con --leer: tope de publicaciones a recorrer (600)')
    p.add_argument('--fase', type=int, default=80, help='con --leer: publicaciones por fase (80); cada fase se guarda y la siguiente retoma donde acabó')
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--sin-comentarios', action='store_true')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    nuevo('lugares', cmd_lugares)
    p = nuevo('lugar', cmd_lugar)
    p.add_argument('nombre')
    p.add_argument('--pagina', action='append', help='página (URL) a seguir; se puede repetir')
    p.add_argument('--grupo', action='append', help='grupo (URL) a seguir; se puede repetir')
    p.add_argument('--busca', action='append', help='texto a buscar en publicaciones de Facebook; se puede repetir')
    p.add_argument('--quitar', action='store_true', help='quitar lo indicado del lugar (sin nada más: borrar el lugar)')
    p.add_argument('--tipo', default='publicaciones', help='tipo de las búsquedas: publicaciones (por defecto), todo, personas, paginas, grupos, lugares...')
    p.add_argument('-n', '--scroll', '--publicaciones', dest='scroll', type=int, help='publicaciones a leer de cada página/grupo (8)')
    p.add_argument('--mostrar', type=int, default=10, help='cuántas de lo último mostrar al final (10)')
    p.add_argument('--vigilar', action='store_true', help='además, añadir sus páginas/grupos a VIGILADOS')
    p.add_argument('--solo-guardar', action='store_true', help='solo guardar la definición, sin leer')
    p.add_argument('--sin-paginas', action='store_true')
    p.add_argument('--sin-buscar', action='store_true')
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    p = nuevo('actividad', cmd_actividad)
    p.add_argument('perfil', nargs='?', help='perfil: Nº de "perfiles", su número de Facebook o parte del nombre (sin indicar: todos)')
    p.add_argument('--leer', action='store_true', help='abrir Facebook y leer antes el Registro de actividad de ese perfil')
    p.add_argument('--categoria', help='apartados a leer con --leer, separados por comas: ' + ', '.join(CATEGORIAS_ACT) + ', todas (por defecto: ' + ', '.join(CATEGORIAS_POR_DEFECTO) + ')')
    p.add_argument('--tipo', help='filtrar: publicacion, comentario, megusta, reaccion, mencion, grupo (varios con comas)')
    p.add_argument('--texto', help='solo lo que contenga este texto')
    opciones_dia(p)
    p.add_argument('-n', type=int, default=40, help='cuántos mostrar (40; 0 = todos)')
    p.add_argument('--scroll', type=int, help='con --leer: pantallas a bajar en cada apartado (6)')
    p.add_argument('--resumen', action='store_true', help='contar por día y tipo en vez de listar')
    p.add_argument('--urls', action='store_true', help='mostrar el enlace de cada elemento')
    p.add_argument('--completo', action='store_true', help='no recortar los textos')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    p = nuevo('perfiles', cmd_perfiles)
    p.add_argument('--anadir', metavar='NÚMERO', help='añadir un perfil por su número de Facebook')
    p.add_argument('nombre', nargs='*', help='con --anadir: nombre para reconocerlo')
    p.add_argument('--quitar', metavar='PERFIL', help='quitar un perfil de la lista')
    p = nuevo('recibido', cmd_recibido)
    p.add_argument('perfil', nargs='?', help='perfil (Nº de "perfiles", número o nombre); sin indicar: todos')
    p.add_argument('--en-otras', action='store_true', help='en vez de tus publicaciones: las publicaciones ajenas donde comentaste, con sus respuestas')
    p.add_argument('--todos', action='store_true', help='con --en-otras: enseñar todos los comentarios, no solo los tuyos y las respuestas')
    p.add_argument('--leer', action='store_true', help='abrir en Facebook esas publicaciones y leer sus comentarios')
    p.add_argument('--detalle', action='store_true', help='con --leer: pedir también quién reaccionó a tus publicaciones')
    p.add_argument('-r', '--reacciones', action='store_true', help='mostrar quién reaccionó (si se consultó)')
    p.add_argument('-e', '--enlaces', action='store_true', help='con -r: enseñar el enlace al perfil de cada persona')
    opciones_dia(p)
    p.add_argument('-n', type=int, default=10, help='cuántas publicaciones (10; 0 = todas)')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    nuevo('diagnostico', cmd_diagnostico)
    p = nuevo('post', cmd_post)
    opciones_extra(p)
    p.add_argument('id', type=int)
    p = nuevo('listas', cmd_listas)
    p.add_argument('tipo', nargs='?')
    p.add_argument('--nuevos', action='store_true')
    p.add_argument('--ambito')
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--bajas', action='store_true', help='ver solo los que ya no sigues (dados de baja por actualizar)')
    p = nuevo('mensajes', cmd_mensajes)
    p.add_argument('conv', nargs='?')
    p.add_argument('--nuevos', action='store_true')
    p = nuevo('repetidas', cmd_repetidas)
    p.add_argument('--aplicar', action='store_true', help='unirlas de verdad (sin esto solo las lista)')
    p = nuevo('ia', cmd_ia)
    p.add_argument('--probar', metavar='IMAGEN', help='probar el OCR y el modelo de visión con una imagen')
    p.add_argument('--activar', action='store_true', help='describir TODAS las imágenes automáticamente al terminar cada  lanzar')
    p.add_argument('--desactivar', action='store_true', help='dejar de describir automáticamente')
    p.add_argument('--ollama', metavar='MODELO', help='usar un modelo de visión de Ollama (p. ej. gemma3:4b); vacío para quitarlo')
    p = nuevo('imgtexto', cmd_imgtexto)
    p.add_argument('ids', nargs='*', help='números de publicación y/o páginas/grupos (P1 G3...)')
    p.add_argument('-n', type=int, help='con páginas/grupos: cuántas publicaciones de cada una (por defecto 5)')
    p.add_argument('--todas', action='store_true', help='también las publicaciones con varias imágenes')
    p.add_argument('--descripcion', action='store_true', help='pedir siempre la descripción (qué se ve) al modelo local, aunque la imagen tenga texto')
    p.add_argument('--rehacer', action='store_true', help='repetir aunque ya estuviera leída')
    p.add_argument('--fotogramas', dest='n_frames', type=int, default=None, help='si la publicación lleva vídeo/reel, sacar N fotogramas (1, 2, 5...) y describirlos también (necesita yt-dlp y ffmpeg)')
    p.add_argument('--pendientes', action='store_true', help='todas las imágenes aún sin leer de la base de datos (las más recientes primero; -n = cuántas publicaciones, 30)')
    p = nuevo('copiar', cmd_copiar)
    p.add_argument('ids', nargs='*', help='números de publicación y/o páginas/grupos (P1 G3...)')
    p.add_argument('-n', type=int, help='con páginas/grupos: cuántas publicaciones de cada una (por defecto 10)')
    p.add_argument('--a', metavar='CARPETA', help='carpeta de destino (por defecto, una nueva dentro de FBVigia/copias)')
    p.add_argument('--con-original', action='store_true', help='incluir también los archivos de la publicación original de las compartidas ya leídas')
    p.add_argument('--sobrescribir', action='store_true')
    p = nuevo('original', cmd_original)
    p.add_argument('ids', nargs='*', help='número(s) de publicación compartida, o páginas/grupos (P1...) con -n')
    p.add_argument('-n', type=int)
    p.add_argument('--sin-detalle', action='store_true')
    p.add_argument('--releer', action='store_true')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=3)
    p.add_argument('--navegador')
    p = nuevo('video', cmd_video)
    p.add_argument('ids', nargs='*', help='números de publicación y/o páginas/grupos (P1 G3...)')
    p.add_argument('-n', type=int, help='con páginas/grupos: cuántas publicaciones de cada una (por defecto 3)')
    p.add_argument('--fotos', dest='n_fotos', type=int, default=3, help='cuántos fotogramas sacar (1, 2, 5...; por defecto 3)')
    p.add_argument('--segundos', type=float, default=5, help='reparte los fotogramas dentro de los primeros N segundos (por defecto 5)')
    p.add_argument('--cookies', help='archivo cookies.txt de Facebook (vídeos que exigen sesión)')
    p.add_argument('--forzar', action='store_true', help='intentarlo aunque el enlace no parezca de vídeo')
    p.add_argument('--describir', action='store_true', help='después de sacarlos, leer su texto y describirlos con la IA local')
    p = nuevo('imagenes', cmd_imagenes)
    p.add_argument('fuente', nargs='?')
    p.add_argument('--renombrar', action='store_true', help='renombrar también las ya descargadas: FECHA-DE-LA-PUBLICACIÓN_Fuente_clave-imgN')
    p.add_argument('-n', type=int, default=60)
    p = nuevo('enlaces', cmd_enlaces)
    p.add_argument('fuente', nargs='?')
    p.add_argument('-n', type=int, default=60)
    p = nuevo('seguir', cmd_seguir)
    p.add_argument('fuente')
    p.add_argument('--max', type=int, default=20)
    p = nuevo('vigilar', cmd_vigilar)
    p.add_argument('urls', nargs='*')
    p.add_argument('--todas', action='store_true')
    p.add_argument('--inicio', action='store_true')
    p.add_argument('--abrir', action='store_true')
    p.add_argument('-n', '--scroll', '--publicaciones', dest='scroll', type=int,
                   help='nº de publicaciones a leer ahora; con --recordar se guarda como valor propio de esa fuente')
    p.add_argument('--recordar', action='store_true', help='con -n: guardar ese número para esa fuente (lanzar lo usará siempre)')
    p.add_argument('--lista', help='encolar todos los elementos de una lista: paginas, grupos, contactos, amigos, personas')
    p.add_argument('--sin-capturar', action='store_true', help='solo apuntarla en vigilados, sin leerla ahora')
    p.add_argument('--max', type=int, default=20)
    p = nuevo('sugerir', cmd_sugerir)
    p.add_argument('--de', help='solo estas listas: contactos,personas,paginas,grupos,amigos')
    p.add_argument('--anadir', action='store_true', help='añadir a vigilados todo lo sugerido')
    p.add_argument('--max', type=int, default=60)
    p.add_argument('--scroll', type=int)
    p = nuevo('actualizar', cmd_actualizar)
    p.add_argument('--solo', help='lista separada por comas: inicio,amigos,personas,paginas,grupos,mensajes,miembros')
    p.add_argument('--scroll', type=int, help='desplazamientos para todas las listas (por defecto cada una el suyo)')
    p.add_argument('--scroll-miembros', type=int, default=25)
    p.add_argument('--sin-miembros', action='store_true')
    p.add_argument('--conversaciones', type=int, default=0, help='además, abrir las N conversaciones más recientes')
    p.add_argument('--fuentes', action='store_true', help='además, las páginas/grupos vigilados')
    p.add_argument('--posts', help='además, capturar las publicaciones de cada elemento de estas listas: paginas,grupos,contactos,personas')
    p.add_argument('--max', type=int, default=20, help='máximo de elementos por lista con --posts')
    p = nuevo('lanzar', cmd_lanzar)
    p.add_argument('objetivos', nargs='*', help='URL, referencia (P3, G2...) o nº de fuente; sin nada: todas las fuentes vigiladas')
    p.add_argument('--de', help='solo fuentes de estas categorías: paginas,grupos,contactos,personas')
    p.add_argument('--listas', action='store_true', help='además, las páginas de listas (inicio, amigos, páginas, grupos...)')
    p.add_argument('-n', '--scroll', '--publicaciones', dest='scroll', type=int,
                   help='nº de PUBLICACIONES a leer de cada página/grupo/persona (por defecto 8 o lo guardado con vigilar -n); también P141:50')
    p.add_argument('--max', type=int, default=0)
    p.add_argument('--espera', type=int, default=240, help='segundos máximos por página (240)')
    p.add_argument('--pausa', type=int, default=4, help='segundos entre páginas (4)')
    p.add_argument('--navegador', help='paquete Android de Firefox (por defecto se detecta; Nightly = org.mozilla.fenix)')
    p.add_argument('--sin-volver', action='store_true', help='no volver a Termux al terminar')
    p.add_argument('--releer', action='store_true', help='leer a fondo (comentarios) también lo ya leído hoy; por defecto cada publicación se lee a fondo una vez al día')
    p.add_argument('--describir', action='store_true', help='al terminar, leer (texto) y DESCRIBIR todas las imágenes de las publicaciones captadas (más lento)')
    p.add_argument('--fotogramas', dest='n_frames', type=int, default=None, help='con --describir: de cada vídeo/reel sacar N fotogramas (1, 2, 5...) y describirlos también')
    p.add_argument('--no-describir', action='store_true', help='no describir aunque esté activado con  ia --activar')
    p = nuevo('abiertas', cmd_abiertas)
    p.add_argument('--scroll', type=int)
    p = nuevo('detalle', cmd_detalle)
    p.add_argument('ids', nargs='*', help='números de publicación y/o páginas/grupos (P1 P2 G3 o su nombre): de cada uno, sus últimas -n publicaciones')
    p.add_argument('-n', '--ultimas', type=int, help='cuántas publicaciones (de cada página dada, o las últimas en general; por defecto 5 por página)')
    p.add_argument('--de')
    p.add_argument('--releer', action='store_true', help='leer otra vez aunque el detalle ya esté completo')
    p.add_argument('--reiniciar', action='store_true', help='olvidar las pasadas anteriores y empezar de nuevo (pasada 1); lo ya guardado no se borra')
    p.add_argument('--pasada', type=int, help='forzar el número de pasada (cada pasada duplica la paciencia y la profundidad)')
    p.add_argument('--foto', action='store_true', help='abrir el enlace guardado tal cual (la foto), sin convertirlo en la dirección de la publicación')
    p.add_argument('--permalink', action='store_true', help='abrir directamente la dirección de la publicación (por defecto se abre su página/grupo y se baja hasta ella)')
    p.add_argument('--abrir', action='store_true', help='(ya es lo normal) abrir Firefox desde Termux y esperar')
    p.add_argument('--cola', action='store_true', help='solo poner en la cola de la extensión, sin abrir Firefox ni mostrar nada')
    p.add_argument('--forzar', action='store_true', help='intentar abrir también los REELS (por defecto se saltan con un aviso)')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=3)
    p.add_argument('--navegador')
    p = nuevo('pub', cmd_pub)
    p.add_argument('urls', nargs='+', help='dirección(es) de la publicación (facebook.com/PÁGINA/posts/...)')
    p.add_argument('--forzar', action='store_true', help='intentar abrir también un REEL (por defecto se salta con un aviso)')
    p.add_argument('--sin-detalle', action='store_true', help='no leer quién reaccionó/compartió, solo la publicación y sus comentarios')
    p.add_argument('--releer', action='store_true', help='leer el detalle aunque ya se haya leído hoy')
    p.add_argument('--en-pagina', action='store_true', help='leer las reacciones desde la página/grupo (baja hasta la publicación) en vez de abrir su dirección suelta')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=3)
    p.add_argument('--navegador')
    p = nuevo('fbuscar', cmd_fbuscar)
    p.add_argument('consulta', nargs='+', help='cada argumento con espacios = frase exacta; argumentos sueltos = palabras')
    p.add_argument('--tipo', default='todo', help='todo, publicaciones, personas, paginas, grupos, videos, fotos, eventos')
    p.add_argument('--exacta', action='store_true', help='buscar cada argumento como frase exacta')
    p.add_argument('--elegir', help='leer directamente estos resultados (1,3 / 2-4 / todos), sin preguntar')
    p.add_argument('--no-preguntar', action='store_true')
    p.add_argument('--sin-abrir', action='store_true', help='solo ponerla en la cola de la extensión')
    p.add_argument('--scroll', type=int, help='desplazamientos de la página de resultados (4) y de cada resultado elegido (config)')
    p.add_argument('-n', type=int, default=5, help='publicaciones a mostrar de cada resultado elegido (5)')
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    opciones_extra(p)
    p = nuevo('resultados', cmd_resultados)
    p.add_argument('busqueda', nargs='?', type=int)
    p.add_argument('--listar', action='store_true', help='lista de búsquedas hechas')
    p = nuevo('elegir', cmd_elegir)
    p.add_argument('seleccion', help='1   1,3,5   2-4   todos')
    p.add_argument('-b', '--busqueda', type=int, help='nº de búsqueda (por defecto la última)')
    p.add_argument('-n', type=int, default=5)
    p.add_argument('--scroll', type=int)
    p.add_argument('--no-marcar', action='store_true')
    p.add_argument('--espera', type=int, default=240)
    p.add_argument('--pausa', type=int, default=4)
    p.add_argument('--navegador')
    opciones_extra(p)
    p = nuevo('cola', cmd_cola)
    p.add_argument('accion', nargs='?', choices=['limpiar', 'reintentar', 'vaciar'])
    p = nuevo('desvigilar', cmd_desvigilar)
    p.add_argument('fuente', nargs='+')
    p.add_argument('--activar', action='store_true')
    p = nuevo('cortadas', cmd_cortadas)
    p.add_argument('--sospechosas', action='store_true', help='incluir textos largos que no acaban en punto ni signo (pista de texto cortado)')
    p.add_argument('--marcar', action='store_true', help='marcarlas para que el próximo lanzar las relea a fondo')
    p.add_argument('-n', type=int, default=30, help='cuántas mostrar (30)')
    p = nuevo('cantidad', cmd_cantidad)
    p.add_argument('pares', nargs='*', help='P3=30 G2=10 12=5 (fuente=publicaciones); sin nada: muestra la tabla')
    p.add_argument('--todas', type=int, help='el mismo número para todas las fuentes vigiladas')
    p = nuevo('vigilados', cmd_vigilados)
    p.add_argument('--solo', nargs='+', help='dejar vigiladas SOLO estas fuentes (Nº, nombre o referencia)')
    p.add_argument('--ninguna', action='store_true', help='vaciar la lista de vigilados')
    p = nuevo('duplicadas', cmd_duplicadas)
    p.add_argument('--unir', action='store_true')
    p = nuevo('unir', cmd_unir)
    p.add_argument('destino')
    p.add_argument('origen')
    p = nuevo('marcar', cmd_marcar)
    p.add_argument('fuente', nargs='?')
    p = nuevo('exportar', cmd_exportar)
    p.add_argument('fuente')
    p.add_argument('-o', '--salida')
    nuevo('estado', cmd_estado)

    if len(sys.argv) < 2 or sys.argv[1] in ('ayuda', 'help', '-h', '--help'):
        print(AYUDA % (VERSION, DIR))
        return
    if len(sys.argv) > 1 and sys.argv[1] not in sp.choices and not sys.argv[1].startswith('-'):
        # abreviaturas: "ultima" -> ultimas; vale cualquier comienzo que solo encaje con una orden
        pos = [c for c in sp.choices if c.startswith(sys.argv[1].lower())]
        if len(pos) == 1:
            sys.argv[1] = pos[0]
        elif len(pos) > 1:
            sys.exit('"%s" puede ser: %s' % (sys.argv[1], ', '.join(pos)))
    a = ap.parse_args()
    if not getattr(a, 'fn', None):
        print(AYUDA % (VERSION, DIR))
        return
    config()
    aplicar_opciones(a)
    a.fn(a)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        pass
