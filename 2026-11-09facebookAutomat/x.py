#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
x.py - X (x.com) desde Termux, con la misma extensión de Firefox que fb.py.

Usa fb.py como biblioteca (deben estar en la misma carpeta) pero con su propia base de datos
(~/x_datos), su propio servidor (puerto 8782) y su propia carpeta de imágenes.
Solo biblioteca estándar de Python.
"""
import argparse
import io
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote_plus, urlparse

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
os.environ.setdefault('FB_DIR', os.path.join(os.path.expanduser('~'), 'x_datos'))
try:
    import fb
except ImportError:
    sys.exit('x.py necesita fb.py en la misma carpeta (%s).' % AQUI)

XVERSION = '1.6'
fb.CFG_DEF['puerto'] = 8782
fb.CFG_DEF['auto_imagenes'] = False          # en X las imágenes se bajan solo si se pide (--imagenes)
fb.CFG_DEF.setdefault('scroll_busqueda', 4)

ESQUEMA_X = """
CREATE TABLE IF NOT EXISTS cuentas(
  id INTEGER PRIMARY KEY, clave TEXT UNIQUE, handle TEXT, nombre TEXT, bio TEXT, url TEXT, uid TEXT,
  sigo INTEGER DEFAULT 0, me_sigue INTEGER DEFAULT 0, verificada INTEGER DEFAULT 0,
  seguidores TEXT, siguiendo TEXT, ubicacion TEXT, web TEXT, alta TEXT,
  primera_vez TEXT, ultima_vez TEXT, origen TEXT);
"""
ESQUEMA_X += """
CREATE TABLE IF NOT EXISTS interacciones(
  id INTEGER PRIMARY KEY, tuit TEXT, tipo TEXT, handle TEXT, nombre TEXT, url TEXT, bio TEXT, primera_vez TEXT,
  UNIQUE(tuit, tipo, handle));
"""
fb.ESQUEMA += ESQUEMA_X


def dir_imagenes_x():
    d = (fb.config().get('carpeta_imagenes') or '').strip()
    if d:
        return os.path.expanduser(d)
    sh = os.path.join(os.path.expanduser('~'), 'storage', 'shared')
    if os.path.isdir(sh) and os.access(sh, os.W_OK):
        return os.path.join(sh, 'Download', 'XVigia', 'imagenes')
    return fb.ruta('imagenes')


fb.dir_imagenes = dir_imagenes_x

# ----------------------------------------------------------------------------
# servidor: el de fb.py + guardado de cuentas
# ----------------------------------------------------------------------------


def guardar_cuentas(d):
    cuentas = list(d.get('cuentas') or [])
    pf = d.get('perfil')
    if pf:
        pf = dict(pf)
        pf['origen'] = 'perfil'
        cuentas.append(pf)
    if not cuentas:
        return 0
    con = fb.db()
    t0 = fb.ahora()
    n = 0
    clave = d.get('clave') or ''
    mq = re.match(r'^x-hilo-(\d+)-(retweets|likes)$', clave)
    with fb.CANDADO:
        if mq:      # lista de quién hizo repost / dio me gusta a un tuit
            for c in cuentas:
                h = (c.get('handle') or '').strip()
                if h and c.get('origen') == clave:
                    con.execute('INSERT OR IGNORE INTO interacciones(tuit,tipo,handle,nombre,url,bio,primera_vez) VALUES(?,?,?,?,?,?,?)',
                                (mq.group(1), mq.group(2), h, c.get('nombre') or h, c.get('url') or 'https://x.com/' + h, (c.get('bio') or '')[:200], t0))
        for c in cuentas:
            c = dict(c)
            if clave.startswith('x-following-') and c.get('origen') == clave:
                c['sigo'] = True        # todas las que salen en «Siguiendo» son cuentas que sigues
            elif clave.startswith('x-followers-') and c.get('origen') == clave:
                c['me_sigue'] = True
            h = (c.get('handle') or '').strip()
            if not h:
                continue
            origen = c.get('origen') or ''
            auto = origen != 'sugerencias'   # las sugerencias no saben si la sigues: no pisan ese dato
            con.execute(
                """INSERT INTO cuentas(clave,handle,nombre,bio,url,uid,sigo,me_sigue,verificada,seguidores,siguiendo,ubicacion,web,alta,primera_vez,ultima_vez,origen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(clave) DO UPDATE SET
                     nombre=COALESCE(NULLIF(excluded.nombre,''),nombre), bio=COALESCE(NULLIF(excluded.bio,''),bio),
                     uid=COALESCE(NULLIF(excluded.uid,''),uid),
                     sigo=CASE WHEN ? THEN excluded.sigo ELSE sigo END, me_sigue=CASE WHEN ? THEN excluded.me_sigue ELSE me_sigue END,
                     verificada=excluded.verificada,
                     seguidores=COALESCE(NULLIF(excluded.seguidores,''),seguidores), siguiendo=COALESCE(NULLIF(excluded.siguiendo,''),siguiendo),
                     ubicacion=COALESCE(NULLIF(excluded.ubicacion,''),ubicacion), web=COALESCE(NULLIF(excluded.web,''),web),
                     alta=COALESCE(NULLIF(excluded.alta,''),alta), ultima_vez=excluded.ultima_vez,
                     origen=CASE WHEN excluded.origen IN ('perfil') THEN origen ELSE excluded.origen END""",
                (h.lower(), h, c.get('nombre'), c.get('bio'), c.get('url') or 'https://x.com/' + h, c.get('id'),
                 1 if c.get('sigo') else 0, 1 if c.get('me_sigue') else 0, 1 if c.get('verificada') else 0,
                 c.get('seguidores'), c.get('siguiendo'), c.get('ubicacion'), c.get('web'), c.get('alta'), t0, t0, origen,
                 1 if auto else 0, 1 if auto else 0))
            n += 1
        con.commit()
    con.close()
    return n


class ManejadorX(fb.Manejador):
    server_version = 'XVigia/' + XVERSION

    def do_POST(self):
        path = urlparse(self.path).path
        if path in ('/captura', '/busqueda'):
            if not self._origen_ok():
                return self._json({'ok': False, 'error': 'origen no permitido'}, 403)
            n = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(n)
            try:
                d = json.loads(body.decode('utf-8'))
                k = guardar_cuentas(d)
                if k:
                    print('[%s] %d cuentas guardadas' % (datetime.now().strftime('%H:%M:%S'), k), flush=True)
            except Exception as e:
                print('aviso: no se pudieron guardar cuentas: %s' % e, flush=True)
            self.rfile = io.BytesIO(body)
        return super().do_POST()


fb.Manejador = ManejadorX      # fb.cmd_servidor y fb.asegurar_servidor usan este nombre


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------

def handle_de(txt):
    """'@pepe', 'pepe', 'https://x.com/pepe' o referencia numérica de 'cuentas' -> handle."""
    t = (txt or '').strip()
    if re.match(r'^\d+$', t):
        con = fb.db()
        r = con.execute('SELECT handle FROM cuentas WHERE id=?', (int(t),)).fetchone()
        con.close()
        if not r:
            sys.exit('No hay ninguna cuenta con el número %s. Mira: python x.py cuentas' % t)
        return r['handle']
    m = re.search(r'(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})', t)
    if m:
        return m.group(1)
    t = t.lstrip('@')
    if not re.match(r'^[A-Za-z0-9_]{1,15}$', t):
        sys.exit('No entiendo la cuenta "%s". Usa @usuario, la URL o su número (python x.py cuentas).' % txt)
    return t


def url_cuenta(h, pestana='publicaciones'):
    base = 'https://x.com/' + h
    return base + {'respuestas': '/with_replies', 'megusta': '/likes'}.get(pestana, '')


def url_busqueda(q, modo):
    f = {'top': '', 'recientes': '&f=live', 'personas': '&f=user', 'medios': '&f=media'}[modo]
    return 'https://x.com/search?q=%s&src=typed_query%s' % (quote_plus(q), f)


def consulta_de(terminos, exacta=False):
    return fb.consulta_de(terminos, exacta)


def ejecutar(url, accion, pub_id, scrolls, nombre, a):
    """Abre la URL en Firefox desde Termux y espera a la extensión. Devuelve (estado, detalle, cola_id)."""
    fb.asegurar_servidor()
    res = fb.lanzar_items([(url, accion, pub_id, scrolls, nombre)], fb.paquete_navegador(a), max(getattr(a, 'espera', 240), int(scrolls or 0) * 10 + 120), 0, volver=True)
    _, estado, det, cid = res[0]
    return estado, det, cid


def fuente_de_cola(con, cid):
    r = con.execute('SELECT c.id, c.fuente_id FROM capturas c WHERE c.cola_id=? ORDER BY c.id DESC LIMIT 1', (cid,)).fetchone()
    return r


def scrolls_de(a, defecto):
    return a.scroll if getattr(a, 'scroll', None) is not None else defecto


def scrolls_para_n(a, defecto, por_pantalla=3):
    """Si no indicas --scroll pero pides muchas publicaciones con -n, sube solo los desplazamientos (~3 por pantalla)."""
    if getattr(a, 'scroll', None) is not None:
        return a.scroll
    n = getattr(a, 'n', 0) or 0
    return max(defecto, -(-n // por_pantalla) + 2) if n else defecto


def bajar_imagenes(a, fid=None):
    if getattr(a, 'imagenes', False):
        fb.descargar_pendientes(fid, log=lambda m: print('   ' + m))


def post_por_url(con, url):
    return con.execute('SELECT * FROM publicaciones WHERE permalink=? ORDER BY id DESC LIMIT 1', (url,)).fetchone()


def mostrar_perfil(con, handle):
    c = con.execute('SELECT * FROM cuentas WHERE clave=?', (handle.lower(),)).fetchone()
    print('=' * fb.ancho())
    if not c:
        print('@%s  (sin datos de perfil todavía)' % handle)
    else:
        print('%s  @%s%s' % (c['nombre'] or handle, c['handle'], '  ✓' if c['verificada'] else ''))
        est = []
        if c['seguidores']:
            est.append('seguidores: %s' % c['seguidores'])
        if c['siguiendo']:
            est.append('siguiendo: %s' % c['siguiendo'])
        est.append('la sigues: %s' % ('sí' if c['sigo'] else 'no'))
        est.append('te sigue: %s' % ('sí' if c['me_sigue'] else 'no'))
        print('  '.join(est))
        for k, e in (('bio', ''), ('ubicacion', 'Lugar: '), ('web', 'Web: '), ('alta', 'Alta: ')):
            if c[k]:
                print(fb.envolver(e + c[k], '  '))
        print(c['url'])
    print('=' * fb.ancho() + '\n')


def mostrar_posts(con, ps, a, marcar=True, com=True):
    for p in ps:
        fb.mostrar_post(con, p, marcar=marcar and not getattr(a, 'no_marcar', False), com=com)
    con.commit()


# ----------------------------------------------------------------------------
# órdenes
# ----------------------------------------------------------------------------

def cmd_servidor(a):
    fb.cmd_servidor(a)


def cmd_cuenta(a):
    """Publicaciones / respuestas / reposts de una cuenta (institución, persona...)."""
    con = fb.db()
    h = handle_de(a.cuenta)
    pest = a.pestana
    url = url_cuenta(h, pest)
    n = a.n
    if not a.guardado:
        print('Abriendo @%s (%s)...' % (h, pest))
        con.close()
        estado, det, cid = ejecutar(url, 'capturar', None, scrolls_para_n(a, int(fb.config()['scrolls'])), '@%s (%s)' % (h, pest), a)
        con = fb.db()
        if estado != 'hecha':
            sys.exit('No se pudo leer la cuenta (%s). Mira: python x.py cola' % (det or estado))
    clave = 'x-' + h.lower() + {'respuestas': '~respuestas', 'megusta': '~megusta'}.get(pest, '')
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()
    mostrar_perfil(con, h)
    if not f:
        print('Todavía no hay publicaciones guardadas de @%s.' % h)
        return
    bajar_imagenes(a, f['id'])
    ps = fb.posts_de(con, f['id'], a.solo_nuevas, 0)
    total_f = len(ps)
    suyo = lambda p: (p['autor_url'] or '').rstrip('/').split('?')[0].lower().rsplit('/', 1)[-1]
    if pest == 'reposts':        # en la pestaña de publicaciones los reposts son tuits de otras cuentas
        ps = [p for p in ps if (p['autor_url'] or '') and suyo(p) != h.lower()]
    elif pest == 'publicaciones':
        ps = [p for p in ps if not (p['autor_url'] or '') or suyo(p) == h.lower()]
    if n:
        ps = ps[:n]
    if not ps:
        print('Nada que mostrar en "%s" (si es "reposts": esta cuenta no tiene reposts entre lo leído).' % pest)
        if total_f:
            print('(Hay %d tuits guardados de esta fuente pero ninguno pasa el filtro de la pestaña; pruébalo con:  python x.py cuenta %s --pestana %s)' % (total_f, h, 'reposts' if pest == 'publicaciones' else 'publicaciones'))
        else:
            print('No se leyó ningún tuit. Si se guardó un informe, mira: python x.py diagnostico  (la carpeta Descargas/FBVigia/diagnosticos) y mándamelo.')
    mostrar_posts(con, ps, a, com=False)



def cmd_avisos(a):
    """Tus avisos de X (x.com/notifications): nuevos seguidores, me gusta, reposts, respuestas, menciones."""
    if not a.guardado:
        url = 'https://x.com/notifications' + ('/mentions' if a.menciones else '')
        print('Abriendo tus avisos de X%s...' % (' (menciones)' if a.menciones else ''))
        estado, det, cid = ejecutar(url, 'capturar', None, scrolls_de(a, 6), 'avisos', a)
        if estado != 'hecha':
            sys.exit('No se pudieron leer los avisos (%s). Mira: python x.py cola' % (det or estado))
    con = fb.db()
    fb.mostrar_avisos(con, 'x', a, 'Los avisos de X solo conservan lo que X enseña en esa pantalla.')


def cmd_actividad(a):
    """Tu actividad en X: lo que publicas, tus respuestas y los tuits a los que das me gusta (+ lo recibido en 'avisos')."""
    con = fb.db()
    h = handle_de(a.cuenta) if a.cuenta else (fb.config().get('yo') or '').lstrip('@')
    if not h:
        sys.exit('Indica tu cuenta:  python x.py actividad @tuusuario   (se recuerda después).')
    if a.cuenta:
        c = fb.config()
        if c.get('yo') != h:
            try:
                cfg = json.load(open(fb.CFG, encoding='utf-8'))
                cfg['yo'] = h
                json.dump(cfg, open(fb.CFG, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
            except Exception:
                pass
    pests = [p.strip() for p in a.pestanas.split(',') if p.strip()]
    for p_ in pests:
        if p_ not in ('publicaciones', 'respuestas', 'megusta'):
            sys.exit('Pestaña desconocida: %s (usa publicaciones, respuestas, megusta)' % p_)
    con.close()
    if not a.guardado:
        for p_ in pests:
            print('Abriendo @%s (%s)...' % (h, p_))
            estado, det, cid = ejecutar(url_cuenta(h, p_), 'capturar', None, scrolls_para_n(a, int(fb.config()['scrolls'])), '@%s (%s)' % (h, p_), a)
            if estado != 'hecha':
                print('  No se pudo leer %s (%s).' % (p_, det or estado))
    con = fb.db()
    tit = {'publicaciones': 'LO QUE PUBLICAS', 'respuestas': 'TUS RESPUESTAS', 'megusta': 'TUITS A LOS QUE DISTE ME GUSTA (X no da la fecha del me gusta)'}
    for p_ in pests:
        clave = 'x-' + h.lower() + {'respuestas': '~respuestas', 'megusta': '~megusta'}.get(p_, '')
        f = con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()
        print('\n' + '=' * fb.ancho() + '\n' + tit[p_] + '\n' + '=' * fb.ancho())
        if not f:
            print('Nada guardado todavía (léelo sin --guardado).')
            continue
        if any(getattr(a, k, None) for k in ('dia', 'desde', 'hasta', 'ultimos_dias')):
            fb.mostrar_dia(con, [f['id']], fb.rangos_de(a), a, tit[p_].lower(), com=False, leido=not a.guardado)
            continue
        ps = fb.posts_de(con, f['id'], False, 0)
        if p_ == 'respuestas':
            ps = [q for q in ps if (q['autor_url'] or '').rstrip('/').lower().rsplit('/', 1)[-1] == h.lower()]
        elif p_ == 'publicaciones':
            ps = [q for q in ps if (q['autor_url'] or '').rstrip('/').lower().rsplit('/', 1)[-1] == h.lower()]
        if a.n:
            ps = ps[:a.n]
        mostrar_posts(con, ps, a, com=False)
    print('\nLo que otros hacen con lo tuyo (seguidores, me gusta, reposts, respuestas): python x.py avisos')


def id_de_tuit(url):
    m = re.search(r'/status/(\d+)', url)
    return m.group(1) if m else ''


QUIENES = {'reposts': ('retweets', 'Hicieron repost'), 'citas': ('quotes', 'Citaron el tuit'), 'megusta': ('likes', 'Dieron me gusta')}


def leer_quienes(url, tid, handle, a):
    """Abre las listas /retweets, /quotes y /likes del tuit y las guarda. Devuelve {clave: (estado, detalle)}."""
    q = (a.quienes or 'reposts,citas,megusta').lower()
    sel = [x.strip() for x in ('reposts,citas,megusta' if q.strip() in ('todo', 'todos', 'todas') else q).split(',') if x.strip()]
    res = {}
    for k in sel:
        if k not in QUIENES:
            sys.exit('--quienes: usa reposts, citas, megusta (separados por comas) o "todo".')
    for k in sel:
        sub, rotulo = QUIENES[k]
        u = 'https://x.com/%s/status/%s/%s' % (handle, tid, sub)
        print('\n-- %s --' % rotulo)
        estado, det, cid = ejecutar(u, 'capturar', None, scrolls_de(a, 25), rotulo, a)
        res[k] = (estado, det)
    return res


def mostrar_quienes(con, tid, a, res):
    print('\n' + '=' * fb.ancho())
    print('QUIÉN HA INTERACTUADO CON EL TUIT %s' % tid)
    print('=' * fb.ancho())
    for k, (sub, rotulo) in QUIENES.items():
        if res is not None and k not in res:
            continue
        if sub == 'quotes':
            f = con.execute('SELECT id FROM fuentes WHERE clave=?', ('x-hilo-%s-quotes' % tid,)).fetchone()
            ps = fb.posts_de(con, f['id'], False, 0) if f else []
            print('\n%s (%d):' % (rotulo, len(ps)))
            for p in ps:
                hm = re.search(r'(?:x|twitter)\.com/([^/?#]+)', p['autor_url'] or '')
                print('  %s @%s  %s' % (p['autor'] or '', hm.group(1) if hm else '', p['autor_url'] or ''))
                if p['texto']:
                    print(fb.envolver(p['texto'], '      '))
                if p['permalink']:
                    print('      ' + p['permalink'])
            if not ps:
                print('  (ninguna cita visible)')
            continue
        rs = con.execute('SELECT * FROM interacciones WHERE tuit=? AND tipo=? ORDER BY id', (tid, sub)).fetchall()
        print('\n%s (%d):' % (rotulo, len(rs)))
        for r in rs:
            print('  %s @%s  %s' % (r['nombre'], r['handle'], r['url']))
        if not rs:
            if sub == 'likes':
                print('  Ninguno visible: desde 2024 X solo enseña la lista de "me gusta" al autor del tuit.')
            elif res is not None and res.get(k, ('hecha',))[0] != 'hecha':
                print('  No se pudo leer la lista (%s).' % (res[k][1] or res[k][0]))
            else:
                print('  (ninguno visible)')
    print('\nVistas: X solo da el número de veces que se ha visto (sale arriba, "vistas"); no publica qué cuentas lo han visto.')


def cmd_hilo(a):
    """Un tuit con su texto, imágenes, respuestas, cifras y quién hizo repost, citó o dio me gusta."""
    con = fb.db()
    url = a.tuit
    if re.match(r'^\d+$', url):
        r = con.execute('SELECT permalink FROM publicaciones WHERE id=?', (int(url),)).fetchone()
        if not r or not r['permalink']:
            sys.exit('La publicación %s no existe o no tiene enlace.' % url)
        url = r['permalink']
    if not re.match(r'^https?://', url):
        sys.exit('Indica la URL del tuit o el nº de una publicación (el #número que sale en las listas).')
    tid = id_de_tuit(url)
    hm = re.search(r'(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status', url)
    handle = hm.group(1) if hm else 'i'
    a.imagenes = not getattr(a, 'sin_imagenes', False)       # en un tuit concreto las imágenes se bajan por defecto
    if not a.guardado:
        con.close()
        estado, det, cid = ejecutar('https://x.com/%s/status/%s' % (handle, tid) if tid else url, 'capturar', None, scrolls_de(a, 6), 'hilo', a)
        con = fb.db()
        fb.OPC['enlaces'] = {1, 2}     # perfil de quien publica y de quienes responden
        if estado != 'hecha':
            print('\n(!) No se pudo leer el tuit en sí: %s.' % (det or estado))
            resumen_diagnostico()
            print('Sigo con las listas de quién hizo repost / citó (son otras páginas de X).\n')
        else:
            mostrar_hilo(con, cid, a)
    else:
        f = con.execute('SELECT id FROM fuentes WHERE clave=?', ('x-hilo-%s' % tid,)).fetchone()
        if not f:
            sys.exit('Ese tuit no está guardado todavía.')
        bajar_imagenes(a, f['id'])
        mostrar_posts(con, fb.posts_de(con, f['id'], False, 1), a, com=True)
    res = None
    if tid and not a.sin_quienes and not a.guardado:
        con.close()
        res = leer_quienes(url, tid, handle, a)
        con = fb.db()
    if tid and not a.sin_quienes:
        mostrar_quienes(con, tid, a, res)


def resumen_diagnostico():
    """Imprime lo esencial del último informe de diagnóstico de la extensión (para saber POR QUÉ no se leyó)."""
    try:
        d = os.path.join(fb.DIR, 'diagnosticos')
        arch = sorted(os.listdir(d))[-1]
        txt = open(os.path.join(d, arch), encoding='utf-8').read()
        j = json.loads(txt[txt.index('{'):])
    except Exception:
        print('   (no hay informe de diagnóstico guardado: probablemente la pestaña no llegó a abrirse o la extensión no respondió;')
        print('    comprueba que Firefox tiene la extensión 1.5.x y "Atender la cola" marcado: python x.py cola)')
        return
    print('   Informe %s:' % arch)
    print('   - página: %s' % (j.get('titulo') or j.get('url')))
    print('   - sesión de X iniciada: %s%s' % ('no' if j.get('login') else 'sí', ' (X pide iniciar sesión)' if j.get('login') else ''))
    print('   - tuits en pantalla: %s   cuentas: %s   tamaño de ventana: %s' % (j.get('tuits'), j.get('cuentas'), j.get('ventana')))
    if j.get('texto_inicio_de_pagina'):
        print('   - texto visible: %s' % j['texto_inicio_de_pagina'][:200])
    print('   Si no lo ves claro, envíame ese archivo: %s' % os.path.join(d, arch))


def mostrar_hilo(con, cid, a):
    c = fuente_de_cola(con, cid)
    if not c:
        print('(la captura no quedó registrada)')
        return
    bajar_imagenes(a, c['fuente_id'])
    ps = fb.posts_de(con, c['fuente_id'], False, 1)
    if not ps:
        print('La página se abrió pero no se encontró el tuit (¿borrado, protegido o X pidió iniciar sesión?).')
        print('Mira el informe:  python x.py diagnostico')
        return
    p = ps[0]
    cif = []
    if p['ncomentarios'] is not None:
        cif.append('respuestas: %s' % p['ncomentarios'])
    if p['ncompartidos'] is not None:
        cif.append('reposts: %s' % p['ncompartidos'])
    if p['reacciones'] is not None:
        cif.append('me gusta: %s' % p['reacciones'])
    if p['visibilidad']:
        cif.append(p['visibilidad'])
    print('CIFRAS DEL TUIT  ' + '   '.join(cif) if cif else 'CIFRAS DEL TUIT  (X no las mostró)')
    mostrar_posts(con, ps, a, com=True)


def cmd_inicio(a):
    con = fb.db()
    print('Abriendo tu inicio de X...')
    con.close()
    estado, det, cid = ejecutar('https://x.com/home', 'capturar', None, scrolls_para_n(a, 6), 'inicio', a)
    con = fb.db()
    if estado != 'hecha':
        sys.exit('No se pudo leer el inicio (%s).' % (det or estado))
    f = con.execute("SELECT * FROM fuentes WHERE clave='x-inicio'").fetchone()
    bajar_imagenes(a, f['id'])
    mostrar_posts(con, fb.posts_de(con, f['id'], a.solo_nuevas, a.n), a, com=False)


def cmd_siguiendo(a):
    con = fb.db()
    if not a.guardado:
        yo = (a.cuenta or fb.config().get('yo') or '').lstrip('@')
        if not yo:
            sys.exit('No sé cuál es tu cuenta. Indícala una vez:  python x.py siguiendo @tuusuario')
        print('Leyendo a quién sigue @%s...' % yo)
        con.close()
        estado, det, cid = ejecutar('https://x.com/%s/following' % yo, 'capturar', None, scrolls_de(a, 25), 'siguiendo', a)
        con = fb.db()
        if estado != 'hecha':
            sys.exit('No se pudo leer la lista (%s).' % (det or estado))
        c = json.load(open(fb.CFG, encoding='utf-8'))
        c['yo'] = yo
        json.dump(c, open(fb.CFG, 'w', encoding='utf-8'), indent=2)
    clave = 'x-following-%s' % (a.cuenta or fb.config().get('yo') or '').lstrip('@').lower()
    filas = con.execute('SELECT * FROM cuentas WHERE origen=? ORDER BY id', (clave,)).fetchall()
    if not filas:
        filas = con.execute('SELECT * FROM cuentas WHERE sigo=1 ORDER BY nombre').fetchall()
    tabla_cuentas(filas)
    print('\nVer las publicaciones de una:  python x.py cuenta Nº   (Nº = primera columna)')


def tabla_cuentas(filas):
    if not filas:
        print('No hay cuentas guardadas.')
        return
    print(fb.tabla(['Nº', 'Usuario', 'Nombre', 'Sigo', 'Te sigue', 'Bio'],
                   [[c['id'], '@' + c['handle'], (c['nombre'] or '')[:30], 'sí' if c['sigo'] else '', 'sí' if c['me_sigue'] else '',
                     (c['bio'] or '').replace('\n', ' ')[:60]] for c in filas]))


def cmd_cuentas(a):
    con = fb.db()
    q = 'SELECT * FROM cuentas'
    par = []
    w = []
    if a.sigo:
        w.append('sigo=1')
    if a.me_sigue:
        w.append('me_sigue=1')
    if a.texto:
        w.append('(lower(handle) LIKE ? OR lower(nombre) LIKE ? OR lower(bio) LIKE ?)')
        par += ['%' + a.texto.lower() + '%'] * 3
    if w:
        q += ' WHERE ' + ' AND '.join(w)
    q += ' ORDER BY id DESC' if not a.sigo else ' ORDER BY nombre'
    if a.n:
        q += ' LIMIT %d' % a.n
    tabla_cuentas(con.execute(q, par).fetchall())


def cmd_seguir(a, seguir=True):
    h = handle_de(a.cuenta)
    accion = 'seguir_cuenta' if seguir else 'dejar_cuenta'
    print('%s @%s...' % ('Siguiendo a' if seguir else 'Dejando de seguir a', h))
    estado, det, cid = ejecutar(url_cuenta(h), accion, None, 0, '@' + h, a)
    if estado != 'hecha':
        sys.exit('No se pudo: %s' % (det or estado))
    print(det or 'Hecho.')
    con = fb.db()
    con.execute('UPDATE cuentas SET sigo=? WHERE clave=?', (1 if seguir else 0, h.lower()))
    con.commit()


def cmd_dejar(a):
    cmd_seguir(a, False)


# ---- buscar --------------------------------------------------------------

MODOS = {'top': 'top', 'destacados': 'top', 'recientes': 'recientes', 'actuales': 'recientes', 'live': 'recientes',
         'personas': 'personas', 'cuentas': 'personas', 'gente': 'personas', 'medios': 'medios'}


def cmd_buscar(a):
    """Buscar en X: una frase entre comillas o palabras sueltas; modos top / recientes / personas."""
    con = fb.db()
    consulta = consulta_de(a.consulta, a.exacta)
    if not consulta:
        sys.exit('Escribe qué buscar:  python x.py buscar "una frase"   o   python x.py buscar palabra otra')
    modo = MODOS.get(a.modo.lower())
    if not modo:
        sys.exit('Modo no válido. Usa: top, recientes, personas, medios')
    url = url_busqueda(consulta, modo)
    cur = con.execute('INSERT INTO busquedas(consulta,tipo,url,creada) VALUES(?,?,?,?)', (consulta, 'x-' + modo, url, fb.ahora()))
    bid = cur.lastrowid
    con.commit()
    sc = scrolls_de(a, int(fb.config().get('scroll_busqueda', 4)))
    print('Buscando en X: %s   [%s]' % (consulta, modo))
    con.close()
    estado, det, cid = ejecutar(url, 'buscar', bid, sc, consulta, a)
    if estado != 'hecha':
        sys.exit('La búsqueda no terminó (%s).  Mira: python x.py cola' % (det or estado))
    con = fb.db()
    mostrar_busqueda(con, bid, a)
    seleccion_interactiva(con, bid, a)


def mostrar_busqueda(con, bid, a):
    b = con.execute('SELECT * FROM busquedas WHERE id=?', (bid,)).fetchone()
    rs = con.execute('SELECT * FROM resultados WHERE busq_id=? ORDER BY n', (bid,)).fetchall()
    print('=' * fb.ancho())
    print('BÚSQUEDA #%d  %s  [%s]  %s' % (b['id'], b['consulta'], b['tipo'], b['creada'][:16]))
    print('=' * fb.ancho())
    if not rs:
        print('Sin resultados leídos. Si X sí los mostró: python x.py diagnostico y mándame el informe.')
        return rs
    if getattr(a, 'imagenes', False):
        bajar_imagenes(a)
    lim = getattr(a, 'n', 0) or len(rs)
    for r in rs[:lim]:
        if r['tipo'] == 'tuit':
            p = post_por_url(con, r['url'])
            print('--- resultado %d ---' % r['n'] + ('  (leído)' if r['leido'] else ''))
            if p:
                fb.mostrar_post(con, p, marcar=not getattr(a, 'no_marcar', False), com=False)
            else:
                print('%s\n%s\n%s\n' % (r['titulo'], fb.envolver(r['detalle'] or ''), r['url']))
        else:
            print('%2d. %s%s  [%s]' % (r['n'], '(leído) ' if r['leido'] else '', r['titulo'], r['tipo']))
            if r['detalle']:
                print(fb.envolver(r['detalle'], '      '))
            if r['url']:
                print('      ' + r['url'])
    con.commit()
    print()
    return rs


def seleccion_interactiva(con, bid, a):
    n = con.execute('SELECT COUNT(*) FROM resultados WHERE busq_id=?', (bid,)).fetchone()[0]
    if not n:
        return
    txt = getattr(a, 'elegir', None)
    if txt is None and sys.stdin.isatty() and not getattr(a, 'no_preguntar', False):
        try:
            txt = input('¿Cuáles quieres abrir? (1   1,3,5   2-4   todos   Intro = ninguno): ')
        except EOFError:
            txt = ''
    if txt:
        abrir_resultados(con, bid, fb.parse_seleccion(txt, n), a)
    else:
        print('Para abrir alguno más tarde:  python x.py elegir 1,3 -b %d' % bid)


def abrir_resultados(con, bid, sel, a):
    """Tuit -> hilo con respuestas; cuenta -> su perfil y publicaciones; sugerencia de búsqueda -> esa búsqueda."""
    rs = {r['n']: r for r in con.execute('SELECT * FROM resultados WHERE busq_id=?', (bid,)).fetchall()}
    for n in sel:
        r = rs.get(n)
        if not r:
            continue
        print('#' * fb.ancho())
        print('%d. %s' % (n, r['titulo']))
        print('#' * fb.ancho())
        con.execute('UPDATE resultados SET leido=1 WHERE busq_id=? AND n=?', (bid, n))
        con.commit()
        if r['tipo'] == 'tuit':
            con.close()
            estado, det, cid = ejecutar(r['url'], 'capturar', None, scrolls_de(a, 6), r['titulo'][:40], a)
            con = fb.db()
            if estado != 'hecha':
                print('  No se pudo leer (%s).' % (det or estado))
                continue
            mostrar_hilo(con, cid, a)
        elif r['tipo'] == 'cuenta':
            con.close()
            args = argparse.Namespace(cuenta=r['url'], pestana=getattr(a, 'pestana', 'publicaciones'), n=getattr(a, 'n', 10) or 10,
                                      scroll=getattr(a, 'scroll', None), guardado=False, solo_nuevas=False,
                                      imagenes=getattr(a, 'imagenes', False), no_marcar=getattr(a, 'no_marcar', False),
                                      espera=getattr(a, 'espera', 240), navegador=getattr(a, 'navegador', None))
            cmd_cuenta(args)
            con = fb.db()
        elif r['tipo'] == 'busqueda':
            con.close()
            args = argparse.Namespace(consulta=[r['titulo']], exacta=False, modo=getattr(a, 'modo', 'top'), scroll=getattr(a, 'scroll', None),
                                      n=getattr(a, 'n', 0), imagenes=getattr(a, 'imagenes', False), elegir=None, no_preguntar=True,
                                      no_marcar=getattr(a, 'no_marcar', False), espera=getattr(a, 'espera', 240), navegador=getattr(a, 'navegador', None))
            cmd_buscar(args)
            con = fb.db()
        else:
            print('  (este tipo de resultado no se puede abrir)')
    con.close()


def cmd_elegir(a):
    con = fb.db()
    bid = a.busqueda or con.execute('SELECT MAX(id) FROM busquedas').fetchone()[0]
    if not bid:
        sys.exit('Aún no has hecho ninguna búsqueda.')
    n = con.execute('SELECT COUNT(*) FROM resultados WHERE busq_id=?', (bid,)).fetchone()[0]
    sel = fb.parse_seleccion(a.seleccion, n)
    if not sel:
        sys.exit('Ningún número válido (la búsqueda #%d tiene %d resultados).' % (bid, n))
    abrir_resultados(con, bid, sel, a)


def cmd_resultados(a):
    con = fb.db()
    if a.listar:
        print(fb.tabla(['Nº', 'Fecha', 'Tipo', 'Resultados', 'Consulta'],
                       [[b['id'], b['creada'][:16], b['tipo'], b['n_resultados'], b['consulta']]
                        for b in con.execute('SELECT * FROM busquedas ORDER BY id DESC LIMIT 30').fetchall()]))
        return
    bid = a.busqueda or con.execute('SELECT MAX(id) FROM busquedas').fetchone()[0]
    if not bid:
        sys.exit('Aún no has hecho ninguna búsqueda.')
    mostrar_busqueda(con, bid, a)


def cmd_sugerir(a):
    """Lo que X propone al escribir en su cuadro de búsqueda (la «lista automática»)."""
    con = fb.db()
    term = ' '.join(a.termino).strip()
    if not term:
        sys.exit('Escribe el término:  python x.py sugerir ciberseguridad')
    url = 'https://x.com/explore?fbv_q=' + quote_plus(term)
    cur = con.execute('INSERT INTO busquedas(consulta,tipo,url,creada) VALUES(?,?,?,?)', (term, 'sugerencias', url, fb.ahora()))
    bid = cur.lastrowid
    con.commit()
    print('Pidiendo a X las sugerencias para «%s»...' % term)
    con.close()
    estado, det, cid = ejecutar(url, 'sugerir', bid, 0, 'sugerencias «%s»' % term, a)
    if estado != 'hecha':
        sys.exit('No terminó (%s).  Mira: python x.py cola' % (det or estado))
    con = fb.db()
    fb.mostrar_resultados(con, bid)
    seleccion_interactiva(con, bid, a)


def cmd_mensajes(a):
    """Mensajes privados de X (SIN PROBAR: no había página de ejemplo)."""
    con = fb.db()
    if a.actualizar:
        print('Abriendo los mensajes de X...')
        con.close()
        estado, det, cid = ejecutar('https://x.com/messages', 'capturar', None, 3, 'mensajes', a)
        con = fb.db()
        if estado != 'hecha':
            sys.exit('No se pudo (%s).' % (det or estado))
        if a.conversaciones:
            cs = con.execute("SELECT * FROM listas WHERE tipo='conversacion' ORDER BY ultima_vez DESC LIMIT ?", (a.conversaciones,)).fetchall()
            con.close()
            for c in cs:
                ejecutar(c['url'], 'capturar', None, 6, 'chat ' + (c['nombre'] or '')[:30], a)
    fb.cmd_mensajes(argparse.Namespace(conv=a.conv, nuevos=a.nuevos))


def cmd_abiertas(a):
    con = fb.db()
    i = fb.encolar(con, '', scrolls_de(a, int(fb.config()['scrolls'])), 'manual', 'abiertas')
    con.commit()
    print('Orden enviada (#%s): la extensión capturará las pestañas de X abiertas.' % i if i else 'Ya había una orden pendiente.')


def _fuente_x(con, clave):
    return con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()


def cmd_dia(a):
    """Publicaciones de una cuenta de X de un día o grupo de días; con --leer las lee antes hacia atrás hasta llegar a esa fecha."""
    rangos = fb.rangos_de(a)
    h = handle_de(a.cuenta)
    pest = a.pestana
    clave = 'x-' + h.lower() + ('~respuestas' if pest == 'respuestas' else '')
    url = url_cuenta(h, pest)

    def propio(p):
        quien = (p['autor_url'] or '').rstrip('/').lower().rsplit('/', 1)[-1]
        if pest == 'reposts':
            return quien != h.lower()
        if pest == 'publicaciones':
            return quien == h.lower()
        return True
    if a.leer:
        d_min = min(d1 for d1, _ in rangos)

        def abrir(n):
            estado, det, cid = ejecutar(url, 'capturar', None, -(-n // 3) + 2, '@%s (%s)' % (h, pest), a)
            return estado == 'hecha', det

        def est():
            c = fb.db()
            f = _fuente_x(c, clave)
            r = fb.estado_ultima_lectura(c, [f['id']] if f else [], propio)
            c.close()
            return r
        fb.leer_hasta_fecha(abrir, est, d_min, a.empezar, a.max)
    con = fb.db()
    f = _fuente_x(con, clave)
    mostrar_perfil(con, h)
    if not f:
        print('Todavía no hay publicaciones guardadas de @%s. Añade --leer para leerla ahora.' % h)
        return
    bajar_imagenes(a, f['id'])
    fb.mostrar_dia(con, [f['id']], rangos, a, '@' + h + ' (' + pest + ')', filtro=propio, com=False, aprox=False, leido=a.leer)


def cmd_lugar(a):
    """Seguir un pueblo o ciudad en X: cuentas (ayuntamiento, medios locales...) y búsquedas de su nombre."""
    k, d = fb.lugar_actualizar_def(a, {'cuenta': 'cuentas', 'busca': 'busquedas'})
    if d is None:
        return
    cuentas = list(d.get('cuentas', []))
    busq = list(d.get('busquedas', []))
    if not cuentas and not busq:
        sys.exit('El lugar "%s" no tiene nada que seguir. Añade --cuenta @usuario o --busca "texto".' % k)
    print('LUGAR: %s   (%d cuentas, %d búsquedas)' % (k, len(cuentas), len(busq)))
    if a.solo_guardar:
        print('Guardado (no se lee nada ahora). Para leerlo: python x.py lugar "%s"' % k)
        return
    comun = dict(imagenes=a.imagenes, no_marcar=a.no_marcar, espera=a.espera, navegador=a.navegador, scroll=a.scroll)
    if not a.sin_cuentas:
        for c in cuentas:
            print('\n' + '#' * fb.ancho() + '\n' + c + '\n' + '#' * fb.ancho())
            try:
                cmd_cuenta(argparse.Namespace(cuenta=c, pestana='publicaciones', n=a.n, guardado=False, solo_nuevas=False, **comun))
            except SystemExit as e:
                print('   (no terminó: %s)' % e)
    if not a.sin_buscar:
        for t in busq:
            print('\n' + '#' * fb.ancho() + '\nBúsqueda: ' + t + '  [' + a.modo + ']\n' + '#' * fb.ancho())
            try:
                cmd_buscar(argparse.Namespace(consulta=[t], exacta=False, modo=a.modo, n=a.n, elegir=None, no_preguntar=True,
                                              pestana='publicaciones', **comun))
            except SystemExit as e:
                print('   (no terminó: %s)' % e)
    print('\nUn día concreto de una cuenta:  python x.py dia @usuario --dia ayer --leer')


XCMDS = {
    'servidor': cmd_servidor, 'cuenta': cmd_cuenta, 'hilo': cmd_hilo, 'inicio': cmd_inicio, 'siguiendo': cmd_siguiendo,
    'cuentas': cmd_cuentas, 'seguir': cmd_seguir, 'dejar': cmd_dejar, 'buscar': cmd_buscar, 'elegir': cmd_elegir,
    'resultados': cmd_resultados, 'sugerir': cmd_sugerir, 'mensajes': cmd_mensajes, 'abiertas': cmd_abiertas,
    'dia': cmd_dia, 'lugar': cmd_lugar, 'avisos': cmd_avisos, 'actividad': cmd_actividad,
}

AYUDA = """X Vigía %s (x.py, usa fb.py %s) - X desde Termux

PREPARACIÓN
  python x.py servidor                         Receptor para X (puerto 8782). Déjalo abierto, o no hace falta:
                                               las órdenes de abajo lo arrancan solas si no está.
  Firefox con la extensión FB Vigía 1.5 y la sesión de X iniciada.

MI ACTIVIDAD Y LO RECIBIDO (1.5)
  python x.py actividad @tuusuario             Lee y muestra lo que publicas, tus respuestas y tus me gusta
  python x.py avisos [--menciones] [--tipo megusta,repost,seguidores] [--dia hoy]   Tus avisos: seguidores, me gusta, reposts, respuestas
  python x.py avisos --guardado                Solo lo ya guardado

BUSCAR (como el cuadro «Buscar» de X)
  python x.py sugerir ciberseguridad           La lista automática que propone X al escribir (cuentas y búsquedas).
  python x.py buscar "una frase"               Frase exacta (entre comillas).
  python x.py buscar palabra otra              Palabras sueltas.
  python x.py buscar ciberseguridad --modo top|recientes|personas
  python x.py buscar ciberseguridad --imagenes -n 10 -e 1,2
     Muestra los resultados numerados y pregunta cuáles abrir (1   1,3   2-4   todos):
     un tuit se abre con sus respuestas; una cuenta, con su perfil y sus publicaciones.
  python x.py resultados [Nº] | --listar       Volver a ver una búsqueda.
  python x.py elegir 1,3 [-b Nº] [--imagenes]  Abrir resultados de una búsqueda ya hecha.

CUENTAS E INSTITUCIONES
  python x.py cuenta @usuario|Nº [--pestana publicaciones|respuestas|reposts|megusta] [-n 10] [--imagenes] [-e 1,2]
  python x.py hilo URL|Nº [--sin-quienes] [--quienes reposts,citas,megusta] [--sin-imagenes] [--guardado]
                                               Un tuit: texto, imágenes (se bajan), cifras (vistas, reposts, citas, me gusta),
                                               respuestas y quién hizo repost, citó o dio me gusta.
  python x.py siguiendo [@tuusuario]           A quién sigues (se guarda); luego: python x.py cuenta Nº
  python x.py cuentas [--sigo] [--me-sigue] [texto]   Cuentas guardadas.
  python x.py seguir @usuario|Nº               Seguir una cuenta.
  python x.py dejar  @usuario|Nº               Dejar de seguirla.

TU INICIO Y MENSAJES
  python x.py inicio [-n 20] [--imagenes]      Tu cronología.
  python x.py mensajes --actualizar [--conversaciones 5] [--nuevos]   Mensajes privados (sin probar).

UN DÍA CONCRETO Y SEGUIR UN PUEBLO
  python x.py dia @usuario --dia ayer --leer      Tuits de esa cuenta de un día (o --desde/--hasta), leyendo hacia atrás si hace falta
  python x.py lugar "La Adrada" --cuenta @ayto --busca "La Adrada"   Seguir un pueblo (cuentas + búsquedas recientes)

LEER LO GUARDADO (igual que fb.py)
  python x.py nuevas | ultimas -n 20 | ver FUENTE | post Nº | fuentes | exportar FUENTE | imagenes | enlaces FUENTE
  python x.py lanzar                           Abre y captura todas las cuentas guardadas como vigiladas.
  python x.py local "texto"                    Busca en lo ya guardado (no en X).
  python x.py estado | cola | diagnostico | abiertas

Opciones comunes: --imagenes (descargar las imágenes y mostrar su ruta), -e 1,2 (enlaces a perfiles: 1=quien
publica, 2=quienes responden), --scroll N, --espera S, --navegador PAQUETE.
Datos en: %s
"""


class _Reescribe(object):
    """Los textos de fb.py hablan de «python fb.py ...»: aquí se muestran como «python x.py ...»."""
    def __init__(self, f):
        self._f = f

    def write(self, t):
        return self._f.write(t.replace('python fb.py', 'python x.py').replace('fb.py post', 'x.py post')
                             .replace('FB Vigía %s' % fb.VERSION, 'X Vigía %s (fb.py %s)' % (XVERSION, fb.VERSION))
                             .replace('Las URL de imágenes de Facebook caducan', 'Las URL de imágenes pueden caducar'))

    def __getattr__(self, k):
        return getattr(self._f, k)


def main():
    fb.proteger_terminal()      # que la terminal de Termux no se quede sin eco (fb.py 1.46)
    sys.stdout = _Reescribe(sys.stdout)
    sys.stderr = _Reescribe(sys.stderr)
    if len(sys.argv) < 2 or sys.argv[1] in ('ayuda', 'help', '-h', '--help'):
        print(AYUDA % (XVERSION, fb.VERSION, fb.DIR))
        return
    cmd = sys.argv[1]
    if cmd not in XCMDS:
        # el resto de órdenes son las de fb.py, sobre los datos de X
        if cmd == 'local':
            sys.argv[1] = 'buscar'
        elif cmd == 'enlaces-seguir':
            sys.argv[1] = 'seguir'
        fb.config()
        return fb.main()
    ap = argparse.ArgumentParser(prog='x.py ' + cmd)
    ap.add_argument('--espera', type=int, default=240)
    ap.add_argument('--navegador')
    ap.add_argument('--scroll', type=int)
    ap.add_argument('--imagenes', action='store_true', help='descargar las imágenes y mostrar su ruta')
    ap.add_argument('-e', '--enlaces', metavar='LISTA', help='enlaces a perfiles: 1=quien publica 2=quienes responden')
    ap.add_argument('--no-marcar', action='store_true')
    if cmd == 'servidor':
        ap.add_argument('--puerto', type=int)
    elif cmd == 'cuenta':
        ap.add_argument('cuenta')
        ap.add_argument('--pestana', '--pestaña', default='publicaciones', choices=['publicaciones', 'respuestas', 'reposts', 'megusta'])
        ap.add_argument('-n', type=int, default=10)
        ap.add_argument('--guardado', action='store_true', help='no abrir X: mostrar lo ya guardado')
        ap.add_argument('--solo-nuevas', action='store_true')
    elif cmd == 'hilo':
        ap.add_argument('tuit')
        ap.add_argument('--guardado', action='store_true', help='no abrir X: mostrar lo ya guardado')
        ap.add_argument('--sin-quienes', action='store_true', help='no leer quién hizo repost / citó / dio me gusta (más rápido)')
        ap.add_argument('--quienes', help='cuáles leer: reposts,citas,megusta (por defecto las tres)')
        ap.add_argument('--sin-imagenes', action='store_true', help='no descargar las imágenes del tuit')
    elif cmd == 'inicio':
        ap.add_argument('-n', type=int, default=20)
        ap.add_argument('--solo-nuevas', action='store_true')
    elif cmd == 'siguiendo':
        ap.add_argument('cuenta', nargs='?')
        ap.add_argument('--guardado', action='store_true')
    elif cmd == 'cuentas':
        ap.add_argument('texto', nargs='?')
        ap.add_argument('--sigo', action='store_true')
        ap.add_argument('--me-sigue', action='store_true')
        ap.add_argument('-n', type=int, default=0)
    elif cmd in ('seguir', 'dejar'):
        ap.add_argument('cuenta')
    elif cmd == 'buscar':
        ap.add_argument('consulta', nargs='+')
        ap.add_argument('--modo', default='top', help='top, recientes, personas, medios')
        ap.add_argument('--exacta', action='store_true')
        ap.add_argument('--elegir')
        ap.add_argument('--no-preguntar', action='store_true')
        ap.add_argument('-n', type=int, default=0, help='cuántos resultados mostrar / publicaciones por cuenta abierta')
        ap.add_argument('--pestana', default='publicaciones')
    elif cmd == 'elegir':
        ap.add_argument('seleccion')
        ap.add_argument('-b', '--busqueda', type=int)
        ap.add_argument('-n', type=int, default=10)
        ap.add_argument('--modo', default='top')
        ap.add_argument('--pestana', default='publicaciones')
    elif cmd == 'resultados':
        ap.add_argument('busqueda', nargs='?', type=int)
        ap.add_argument('--listar', action='store_true')
        ap.add_argument('-n', type=int, default=0)
    elif cmd == 'sugerir':
        ap.add_argument('termino', nargs='+')
        ap.add_argument('--elegir')
        ap.add_argument('--no-preguntar', action='store_true')
        ap.add_argument('--modo', default='top')
        ap.add_argument('-n', type=int, default=10)
    elif cmd == 'dia':
        ap.add_argument('cuenta')
        fb.opciones_dia(ap)
        ap.add_argument('--pestana', '--pestaña', default='publicaciones', choices=['publicaciones', 'respuestas', 'reposts'])
        ap.add_argument('--leer', action='store_true', help='leer la cuenta en X hacia atrás hasta llegar a esa fecha')
        ap.add_argument('--empezar', type=int, default=20, help='con --leer: tuits de la primera lectura (20); si no llega se duplica')
        ap.add_argument('--max', type=int, default=150, help='con --leer: tope de tuits a pedir (150)')
    elif cmd == 'lugar':
        ap.add_argument('nombre')
        ap.add_argument('--cuenta', action='append', help='cuenta de X a seguir (@usuario); se puede repetir')
        ap.add_argument('--busca', action='append', help='texto a buscar en X; se puede repetir')
        ap.add_argument('--quitar', action='store_true', help='quitar lo indicado (sin nada más: borrar el lugar)')
        ap.add_argument('--modo', default='recientes', help='modo de las búsquedas: top, recientes (por defecto), personas, medios')
        ap.add_argument('-n', type=int, default=10, help='tuits a mostrar de cada cuenta/búsqueda (10)')
        ap.add_argument('--solo-guardar', action='store_true')
        ap.add_argument('--sin-cuentas', action='store_true')
        ap.add_argument('--sin-buscar', action='store_true')
    elif cmd == 'avisos':
        ap.add_argument('--guardado', action='store_true', help='no abrir X: mostrar lo ya guardado')
        ap.add_argument('--menciones', action='store_true', help='leer la pestaña «Menciones» en vez de «Todo»')
        ap.add_argument('--tipo', help='filtrar: seguidores, megusta, reposts, citas, respuestas, menciones (varios con comas)')
        fb.opciones_dia(ap)
        ap.add_argument('-n', type=int, default=40, help='cuántos mostrar (40; 0 = todos)')
        ap.add_argument('--urls', action='store_true', help='enseñar el enlace del tuit de cada aviso')
    elif cmd == 'actividad':
        ap.add_argument('cuenta', nargs='?', help='tu @usuario (se recuerda la primera vez)')
        ap.add_argument('--pestanas', default='publicaciones,respuestas,megusta', help='qué leer: publicaciones,respuestas,megusta (todas por defecto)')
        ap.add_argument('--guardado', action='store_true', help='no abrir X: mostrar lo ya guardado')
        fb.opciones_dia(ap)
        ap.add_argument('-n', type=int, default=10, help='cuántos tuits mostrar de cada pestaña (10; 0 = todos)')
        ap.add_argument('--solo-nuevas', action='store_true')
    elif cmd == 'mensajes':
        ap.add_argument('conv', nargs='?')
        ap.add_argument('--actualizar', action='store_true')
        ap.add_argument('--conversaciones', type=int, default=0)
        ap.add_argument('--nuevos', action='store_true')
    a = ap.parse_args(sys.argv[2:])
    fb.config()
    fb.aplicar_opciones(a)
    XCMDS[cmd](a)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        pass
