#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ig.py - Instagram desde Termux, con la misma extensión de Firefox que fb.py y x.py.

Usa fb.py como biblioteca (deben estar en la misma carpeta) pero con su propia base de datos
(~/ig_datos), su propio servidor (puerto 8783) y su propia carpeta de imágenes.
Solo biblioteca estándar de Python.
"""
import argparse
import io
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote_plus, urlparse

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
os.environ.setdefault('FB_DIR', os.path.join(os.path.expanduser('~'), 'ig_datos'))
try:
    import fb
except ImportError:
    sys.exit('ig.py necesita fb.py en la misma carpeta (%s).' % AQUI)

IVERSION = '1.2'
fb.CFG_DEF['puerto'] = 8783
fb.CFG_DEF['auto_imagenes'] = False          # las imágenes se bajan solo si se pide (--imagenes)
fb.CFG_DEF.setdefault('scroll_busqueda', 2)

ESQUEMA_IG = """
CREATE TABLE IF NOT EXISTS cuentas(
  id INTEGER PRIMARY KEY, clave TEXT UNIQUE, handle TEXT, nombre TEXT, bio TEXT, url TEXT, uid TEXT,
  sigo INTEGER DEFAULT 0, me_sigue INTEGER DEFAULT 0, verificada INTEGER DEFAULT 0,
  seguidores TEXT, siguiendo TEXT, ubicacion TEXT, web TEXT, alta TEXT, n_publ TEXT,
  primera_vez TEXT, ultima_vez TEXT, origen TEXT);
"""
fb.ESQUEMA += ESQUEMA_IG

_norm_orig = fb.norm_url


def norm_ig(u):
    """En Instagram la cola se identifica solo por la ruta (Instagram quita o cambia los parámetros de la URL)."""
    try:
        p = urlparse(u if '//' in u else 'https://' + u)
    except Exception:
        return u or ''
    host = p.netloc.lower()
    if host.endswith('instagram.com'):
        return 'instagram.com' + re.sub(r'/+$', '', p.path)
    return _norm_orig(u)


fb.norm_url = norm_ig


def dir_imagenes_ig():
    d = (fb.config().get('carpeta_imagenes') or '').strip()
    if d:
        return os.path.expanduser(d)
    sh = os.path.join(os.path.expanduser('~'), 'storage', 'shared')
    if os.path.isdir(sh) and os.access(sh, os.W_OK):
        return os.path.join(sh, 'Download', 'IGVigia', 'imagenes')
    return fb.ruta('imagenes')


fb.dir_imagenes = dir_imagenes_ig

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
    with fb.CANDADO:
        for c in cuentas:
            c = dict(c)
            if clave.startswith('ig-following-') and c.get('origen') == clave:
                c['sigo'] = True        # todas las que salen en «Seguidos» son cuentas que sigues
            elif clave.startswith('ig-followers-') and c.get('origen') == clave:
                c['me_sigue'] = True
            h = (c.get('handle') or '').strip()
            if not h:
                continue
            origen = c.get('origen') or ''
            auto = origen not in ('ig-explorar', 'ig-busqueda')   # los resultados de búsqueda no saben si la sigues
            con.execute(
                """INSERT INTO cuentas(clave,handle,nombre,bio,url,uid,sigo,me_sigue,verificada,seguidores,siguiendo,ubicacion,web,alta,n_publ,primera_vez,ultima_vez,origen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(clave) DO UPDATE SET
                     nombre=COALESCE(NULLIF(excluded.nombre,''),nombre), bio=COALESCE(NULLIF(excluded.bio,''),bio),
                     sigo=CASE WHEN ? THEN excluded.sigo ELSE sigo END, me_sigue=CASE WHEN ? THEN excluded.me_sigue ELSE me_sigue END,
                     verificada=excluded.verificada,
                     seguidores=COALESCE(NULLIF(excluded.seguidores,''),seguidores), siguiendo=COALESCE(NULLIF(excluded.siguiendo,''),siguiendo),
                     web=COALESCE(NULLIF(excluded.web,''),web), n_publ=COALESCE(NULLIF(excluded.n_publ,''),n_publ),
                     ultima_vez=excluded.ultima_vez,
                     origen=CASE WHEN excluded.origen IN ('perfil') THEN origen ELSE excluded.origen END""",
                (h.lower(), h, c.get('nombre'), c.get('bio'), c.get('url') or 'https://www.instagram.com/' + h + '/', c.get('id'),
                 1 if c.get('sigo') else 0, 1 if c.get('me_sigue') else 0, 1 if c.get('verificada') else 0,
                 c.get('seguidores'), c.get('siguiendo'), c.get('ubicacion'), c.get('web'), c.get('alta'), c.get('publicaciones'), t0, t0, origen,
                 1 if auto else 0, 1 if auto else 0))
            n += 1
        con.commit()
    con.close()
    return n


class ManejadorIG(fb.Manejador):
    server_version = 'IGVigia/' + IVERSION

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


fb.Manejador = ManejadorIG      # fb.cmd_servidor y fb.asegurar_servidor usan este nombre


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------

def handle_de(txt):
    """'@pepe', 'pepe', 'https://www.instagram.com/pepe/' o referencia numérica de 'cuentas' -> usuario."""
    t = (txt or '').strip()
    if re.match(r'^\d+$', t):
        con = fb.db()
        r = con.execute('SELECT handle FROM cuentas WHERE id=?', (int(t),)).fetchone()
        con.close()
        if not r:
            sys.exit('No hay ninguna cuenta con el número %s. Mira: python ig.py cuentas' % t)
        return r['handle']
    m = re.search(r'instagram\.com/([A-Za-z0-9._]{1,30})', t)
    if m:
        return m.group(1)
    t = t.lstrip('@')
    if not re.match(r'^[A-Za-z0-9._]{1,30}$', t):
        sys.exit('No entiendo la cuenta "%s". Usa @usuario, la URL o su número (python ig.py cuentas).' % txt)
    return t


def url_cuenta(h, pestana='publicaciones'):
    base = 'https://www.instagram.com/' + h + '/'
    return base + {'reels': 'reels/', 'etiquetadas': 'tagged/'}.get(pestana, '')


def url_hilo(t):
    """URL, código (DeKRLwdAh1p) o nº de una publicación guardada -> URL de la publicación."""
    t = (t or '').strip()
    if re.match(r'^https?://', t):
        return t
    if re.match(r'^\d+$', t):
        con = fb.db()
        r = con.execute('SELECT permalink FROM publicaciones WHERE id=?', (int(t),)).fetchone()
        con.close()
        if not r or not r['permalink']:
            sys.exit('La publicación %s no existe o no tiene enlace.' % t)
        return r['permalink']
    if re.match(r'^[A-Za-z0-9_-]{6,}$', t):
        return 'https://www.instagram.com/p/%s/' % t
    sys.exit('Indica la URL de la publicación, su código o el nº (#número) de una publicación guardada.')


def ejecutar(url, accion, pub_id, scrolls, nombre, a):
    """Abre la URL en Firefox desde Termux y espera a la extensión. Devuelve (estado, detalle, cola_id)."""
    fb.asegurar_servidor()
    res = fb.lanzar_items([(url, accion, pub_id, scrolls, nombre)], fb.paquete_navegador(a), max(getattr(a, 'espera', 240), int(scrolls or 0) * 10 + 120), 0, volver=True)
    _, estado, det, cid = res[0]
    return estado, det, cid


def fuente_de_cola(con, cid):
    return con.execute('SELECT c.id, c.fuente_id FROM capturas c WHERE c.cola_id=? ORDER BY c.id DESC LIMIT 1', (cid,)).fetchone()


def scrolls_de(a, defecto):
    return a.scroll if getattr(a, 'scroll', None) is not None else defecto


def scrolls_para_n(a, defecto, por_pantalla=6):
    """Si no indicas --scroll pero pides muchas publicaciones con -n, sube solo los desplazamientos (~6 por pantalla)."""
    if getattr(a, 'scroll', None) is not None:
        return a.scroll
    n = getattr(a, 'n', 0) or 0
    return max(defecto, -(-n // por_pantalla) + 2) if n else defecto


def bajar_imagenes(a, fid=None):
    if getattr(a, 'imagenes', False):
        fb.descargar_pendientes(fid, log=lambda m: print('   ' + m))


def mostrar_perfil(con, handle):
    c = con.execute('SELECT * FROM cuentas WHERE clave=?', (handle.lower(),)).fetchone()
    print('=' * fb.ancho())
    if not c:
        print('@%s  (sin datos de perfil todavía)' % handle)
    else:
        print('%s  @%s%s' % (c['nombre'] or handle, c['handle'], '  ✓' if c['verificada'] else ''))
        est = []
        if c['n_publ']:
            est.append('publicaciones: %s' % c['n_publ'])
        if c['seguidores']:
            est.append('seguidores: %s' % c['seguidores'])
        if c['siguiendo']:
            est.append('seguidos: %s' % c['siguiendo'])
        est.append('la sigues: %s' % ('sí' if c['sigo'] else 'no'))
        print('  '.join(est))
        for k, e in (('bio', ''), ('web', 'Web: ')):
            if c[k]:
                print(fb.envolver(e + c[k], '  '))
        print(c['url'])
    print('=' * fb.ancho() + '\n')


def mostrar_posts(con, ps, a, marcar=True, com=True):
    for p in ps:
        fb.mostrar_post(con, p, marcar=marcar and not getattr(a, 'no_marcar', False), com=com)
    con.commit()


def nueva_lista(con, consulta, tipo, url, filas):
    """Crea una «búsqueda» con filas (tipo, titulo, detalle, url) para poder elegir por número."""
    cur = con.execute('INSERT INTO busquedas(consulta,tipo,url,creada) VALUES(?,?,?,?)', (consulta, tipo, url, fb.ahora()))
    bid = cur.lastrowid
    n = 0
    for tp, titulo, detalle, u in filas:
        n += 1
        con.execute('INSERT OR IGNORE INTO resultados(busq_id,n,tipo,titulo,detalle,url,norm) VALUES(?,?,?,?,?,?,?)',
                    (bid, n, tp, titulo, detalle, u, fb.norm_url(u) if u else titulo))
    con.execute('UPDATE busquedas SET n_resultados=? WHERE id=?', (n, bid))
    con.commit()
    return bid


def filas_de_posts(ps):
    out = []
    for p in ps:
        quien = (p['autor'] or '').lstrip('@')
        det = re.sub(r'^\[miniatura\]\s*', '', (p['texto'] or '').replace('\n', ' '))
        out.append(('publicacion', ('@' + quien) if quien and not quien.startswith('(') else '(publicación)', det[:240], p['permalink']))
    return out


# ----------------------------------------------------------------------------
# órdenes
# ----------------------------------------------------------------------------

def cmd_servidor(a):
    fb.cmd_servidor(a)


def cmd_cuenta(a):
    """Perfil + publicaciones de una cuenta (persona, institución...); se pueden leer completas (con comentarios)."""
    con = fb.db()
    h = handle_de(a.cuenta)
    pest = a.pestana
    url = url_cuenta(h, pest)
    if not a.guardado:
        print('Abriendo @%s (%s)...' % (h, pest))
        con.close()
        estado, det, cid = ejecutar(url, 'capturar', None, scrolls_para_n(a, int(fb.config()['scrolls'])), '@%s (%s)' % (h, pest), a)
        con = fb.db()
        if estado != 'hecha':
            sys.exit('No se pudo leer la cuenta (%s). Mira: python ig.py cola' % (det or estado))
    clave = 'ig-' + h.lower() + ('~reels' if pest == 'reels' else '~tagged' if pest == 'etiquetadas' else '')
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()
    mostrar_perfil(con, h)
    if not f:
        print('Todavía no hay publicaciones guardadas de @%s.' % h)
        return
    ps = fb.posts_de(con, f['id'], a.solo_nuevas, 0)
    if a.n:
        ps = ps[:a.n]
    if not ps:
        print('Nada que mostrar en "%s" (¿cuenta privada o sin publicaciones?).' % pest)
        return
    bajar_imagenes(a, f['id'])
    bid = nueva_lista(con, '@%s (%s)' % (h, pest), 'ig-cuenta', url, filas_de_posts(ps))
    mostrar_lista_posts(con, bid)
    seleccion_interactiva(con, bid, a, 'leer')


def mostrar_lista_posts(con, bid):
    rs = con.execute('SELECT * FROM resultados WHERE busq_id=? ORDER BY n', (bid,)).fetchall()
    for r in rs:
        print('%2d. %s%s' % (r['n'], '(leído) ' if r['leido'] else '', r['titulo']))
        if r['detalle']:
            print(fb.envolver(r['detalle'], '      '))
        print('      ' + (r['url'] or ''))
    print()


def cmd_hilo(a):
    """Una publicación con sus comentarios."""
    url = url_hilo(a.publicacion)
    estado, det, cid = ejecutar(url, 'capturar', None, scrolls_de(a, 8), 'publicación', a)
    con = fb.db()
    if estado != 'hecha':
        sys.exit('No se pudo leer la publicación (%s).' % (det or estado))
    mostrar_hilo(con, cid, a)


def mostrar_hilo(con, cid, a):
    c = fuente_de_cola(con, cid)
    if not c:
        print('(la captura no quedó registrada)')
        return
    bajar_imagenes(a, c['fuente_id'])
    ps = fb.posts_de(con, c['fuente_id'], False, 1)
    mostrar_posts(con, ps, a, com=True)


def cmd_inicio(a):
    con = fb.db()
    print('Abriendo tu inicio de Instagram%s...' % (' (Siguiendo)' if a.siguiendo else ''))
    con.close()
    estado, det, cid = ejecutar('https://www.instagram.com/' + ('?variant=following' if a.siguiendo else ''), 'capturar', None, scrolls_para_n(a, 6), 'inicio', a)
    con = fb.db()
    if estado != 'hecha':
        sys.exit('No se pudo leer el inicio (%s).' % (det or estado))
    f = con.execute("SELECT * FROM fuentes WHERE clave=?", ('ig-inicio-siguiendo' if a.siguiendo else 'ig-inicio',)).fetchone()
    if not f:
        sys.exit('El inicio no devolvió publicaciones.')
    bajar_imagenes(a, f['id'])
    mostrar_posts(con, fb.posts_de(con, f['id'], a.solo_nuevas, a.n), a, com=False)


def cmd_etiqueta(a):
    """Publicaciones destacadas de una etiqueta (#hashtag)."""
    tag = a.etiqueta.lstrip('#').lower()
    print('Abriendo #%s...' % tag)
    estado, det, cid = ejecutar('https://www.instagram.com/explore/tags/%s/' % quote_plus(tag), 'capturar', None, scrolls_de(a, 3), '#' + tag, a)
    con = fb.db()
    if estado != 'hecha':
        sys.exit('No se pudo leer la etiqueta (%s).' % (det or estado))
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', ('ig-tag-' + tag,)).fetchone()
    if not f:
        print('La etiqueta no devolvió publicaciones.')
        return
    ps = fb.posts_de(con, f['id'], False, a.n or 0)
    bajar_imagenes(a, f['id'])
    bid = nueva_lista(con, '#' + tag, 'ig-etiqueta', 'https://www.instagram.com/explore/tags/%s/' % tag, filas_de_posts(ps))
    mostrar_lista_posts(con, bid)
    seleccion_interactiva(con, bid, a, 'leer')


def cmd_siguiendo(a, seguidores=False):
    con = fb.db()
    vista = 'followers' if seguidores else 'following'
    if not a.guardado:
        yo = (a.cuenta or fb.config().get('yo') or '').lstrip('@')
        if not yo:
            sys.exit('No sé cuál es tu cuenta. Indícala una vez:  python ig.py %s @tuusuario' % ('seguidores' if seguidores else 'siguiendo'))
        print('Leyendo %s @%s...' % ('los seguidores de' if seguidores else 'a quién sigue', yo))
        con.close()
        estado, det, cid = ejecutar('https://www.instagram.com/%s/%s/' % (yo, vista), 'capturar', None, scrolls_de(a, 25), vista, a)
        con = fb.db()
        if estado != 'hecha':
            sys.exit('No se pudo leer la lista (%s).' % (det or estado))
        c = json.load(open(fb.CFG, encoding='utf-8'))
        c['yo'] = yo
        json.dump(c, open(fb.CFG, 'w', encoding='utf-8'), indent=2)
    clave = 'ig-%s-%s' % (vista, (a.cuenta or fb.config().get('yo') or '').lstrip('@').lower())
    filas = con.execute('SELECT * FROM cuentas WHERE origen=? ORDER BY id', (clave,)).fetchall()
    if not filas:
        filas = con.execute('SELECT * FROM cuentas WHERE %s=1 ORDER BY nombre' % ('me_sigue' if seguidores else 'sigo')).fetchall()
    tabla_cuentas(filas)
    print('\nVer las publicaciones de una:  python ig.py cuenta Nº   (Nº = primera columna)')


def cmd_seguidores(a):
    cmd_siguiendo(a, True)


def tabla_cuentas(filas):
    if not filas:
        print('No hay cuentas guardadas.')
        return
    print(fb.tabla(['Nº', 'Usuario', 'Nombre', 'Sigo', 'Te sigue', 'Seguidores'],
                   [[c['id'], '@' + c['handle'], (c['nombre'] or '')[:32], 'sí' if c['sigo'] else '', 'sí' if c['me_sigue'] else '',
                     (c['seguidores'] or '')[:12]] for c in filas]))


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

def hacer_busqueda(termino, tipo, scrolls, a):
    con = fb.db()
    url = 'https://www.instagram.com/explore/?fbv_q=' + quote_plus(termino)
    cur = con.execute('INSERT INTO busquedas(consulta,tipo,url,creada) VALUES(?,?,?,?)', (termino, tipo, url, fb.ahora()))
    bid = cur.lastrowid
    con.commit()
    con.close()
    estado, det, cid = ejecutar(url, 'buscar', bid, scrolls, termino, a)
    if estado != 'hecha':
        sys.exit('La búsqueda no terminó (%s).  Mira: python ig.py cola' % (det or estado))
    return bid


def cmd_buscar(a):
    """Buscar en Instagram como en su cuadro de búsqueda: cuentas, etiquetas y lugares."""
    consulta = fb.consulta_de(a.consulta, a.exacta).strip('"')
    if not consulta:
        sys.exit('Escribe qué buscar:  python ig.py buscar "una frase"   o   python ig.py buscar palabra otra')
    print('Buscando en Instagram: %s' % consulta)
    bid = hacer_busqueda(consulta, 'ig-busqueda', scrolls_de(a, int(fb.config().get('scroll_busqueda', 2))), a)
    con = fb.db()
    mostrar_busqueda(con, bid, a)
    seleccion_interactiva(con, bid, a, 'abrir')


def cmd_sugerir(a):
    """La lista automática que sale al escribir en el cuadro de búsqueda (sin desplazarla)."""
    term = ' '.join(a.termino).strip()
    if not term:
        sys.exit('Escribe el término:  python ig.py sugerir ciberseguridad')
    print('Pidiendo a Instagram las sugerencias para «%s»...' % term)
    bid = hacer_busqueda(term, 'ig-sugerencias', 0, a)
    con = fb.db()
    mostrar_busqueda(con, bid, a)
    seleccion_interactiva(con, bid, a, 'abrir')


def mostrar_busqueda(con, bid, a):
    b = con.execute('SELECT * FROM busquedas WHERE id=?', (bid,)).fetchone()
    rs = con.execute('SELECT * FROM resultados WHERE busq_id=? ORDER BY n', (bid,)).fetchall()
    print('=' * fb.ancho())
    print('BÚSQUEDA #%d  %s  [%s]  %s' % (b['id'], b['consulta'], b['tipo'], b['creada'][:16]))
    print('=' * fb.ancho())
    if not rs:
        print('Sin resultados leídos. Si Instagram sí los mostró: python ig.py diagnostico y mándame el informe.')
        return rs
    lim = getattr(a, 'n', 0) or len(rs)
    for r in rs[:lim]:
        print('%2d. %s%s  [%s]' % (r['n'], '(leído) ' if r['leido'] else '', r['titulo'], r['tipo']))
        if r['detalle']:
            print(fb.envolver(r['detalle'], '      '))
        if r['url']:
            print('      ' + r['url'])
    con.commit()
    print()
    return rs


def seleccion_interactiva(con, bid, a, verbo='abrir'):
    n = con.execute('SELECT COUNT(*) FROM resultados WHERE busq_id=?', (bid,)).fetchone()[0]
    if not n:
        return
    txt = getattr(a, 'elegir', None)
    if txt is None and sys.stdin.isatty() and not getattr(a, 'no_preguntar', False):
        try:
            txt = input('¿Cuáles quieres %s? (1   1,3,5   2-4   todos   Intro = ninguno): ' % verbo)
        except EOFError:
            txt = ''
    if txt:
        abrir_resultados(con, bid, fb.parse_seleccion(txt, n), a)
    else:
        print('Para abrir alguno más tarde:  python ig.py elegir 1,3 -b %d' % bid)


def abrir_resultados(con, bid, sel, a):
    """Publicación -> con sus comentarios; cuenta -> su perfil y publicaciones; etiqueta -> sus publicaciones."""
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
        con.close()
        if r['tipo'] == 'publicacion':
            estado, det, cid = ejecutar(r['url'], 'capturar', None, scrolls_de(a, 8), r['titulo'][:40], a)
            con = fb.db()
            if estado != 'hecha':
                print('  No se pudo leer (%s).' % (det or estado))
                continue
            mostrar_hilo(con, cid, a)
        elif r['tipo'] == 'cuenta':
            args = argparse.Namespace(cuenta=r['url'], pestana=getattr(a, 'pestana', 'publicaciones'), n=getattr(a, 'n', 10) or 10,
                                      scroll=getattr(a, 'scroll', None), guardado=False, solo_nuevas=False, elegir=getattr(a, 'leer', None),
                                      no_preguntar=getattr(a, 'leer', None) is None, imagenes=getattr(a, 'imagenes', False),
                                      no_marcar=getattr(a, 'no_marcar', False), espera=getattr(a, 'espera', 240), navegador=getattr(a, 'navegador', None))
            cmd_cuenta(args)
            con = fb.db()
        elif r['tipo'] == 'etiqueta':
            args = argparse.Namespace(etiqueta=r['titulo'], scroll=getattr(a, 'scroll', None), n=getattr(a, 'n', 0), elegir=getattr(a, 'leer', None),
                                      no_preguntar=getattr(a, 'leer', None) is None, imagenes=getattr(a, 'imagenes', False),
                                      no_marcar=getattr(a, 'no_marcar', False), espera=getattr(a, 'espera', 240), navegador=getattr(a, 'navegador', None))
            cmd_etiqueta(args)
            con = fb.db()
        else:
            con = fb.db()
            print('  (este tipo de resultado no se puede abrir desde aquí; ábrelo en Firefox: %s)' % r['url'])
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


# ---- actividad ------------------------------------------------------------

ACT = {'megusta': 'likes', 'me-gusta': 'likes', 'likes': 'likes', 'comentarios': 'comments', 'comments': 'comments',
       'republicaciones': 'reposts', 'reposts': 'reposts', 'historias': 'story_replies', 'respuestas': 'story_replies',
       'opiniones': 'reviews'}


def cmd_actividad(a):
    """Tu actividad: me gusta, comentarios, republicaciones... (Instagram > Tu actividad > Interacciones)."""
    k = ACT.get((a.tipo or 'megusta').lower().replace(' ', '-'))
    if not k:
        sys.exit('Tipo no válido. Usa: megusta, comentarios, republicaciones, historias, opiniones')
    print('Abriendo tu actividad (%s)...' % k)
    estado, det, cid = ejecutar('https://www.instagram.com/your_activity/interactions/%s/' % k, 'capturar', None, scrolls_de(a, 10), 'actividad ' + k, a)
    con = fb.db()
    if estado != 'hecha':
        sys.exit('No se pudo leer la actividad (%s).' % (det or estado))
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', ('ig-act-' + k,)).fetchone()
    if not f:
        print('No se leyó nada en esa pestaña.')
        return
    bajar_imagenes(a, f['id'])
    mostrar_posts(con, fb.posts_de(con, f['id'], a.solo_nuevas, a.n), a, com=True)


# ---- mensajes -------------------------------------------------------------

def cmd_mensajes(a):
    """Mensajes directos de Instagram (la lista y las N primeras conversaciones). SIN PROBAR con una conversación real."""
    if a.actualizar:
        print('Abriendo los mensajes de Instagram...')
        estado, det, cid = ejecutar('https://www.instagram.com/direct/inbox/', 'mensajes', None, a.conversaciones, 'mensajes', a)
        if estado != 'hecha':
            sys.exit('No se pudo (%s).' % (det or estado))
        if det:
            print(det)
    fb.cmd_mensajes(argparse.Namespace(conv=a.conv, nuevos=a.nuevos))


def cmd_abiertas(a):
    con = fb.db()
    i = fb.encolar(con, '', scrolls_de(a, int(fb.config()['scrolls'])), 'manual', 'abiertas')
    con.commit()
    print('Orden enviada (#%s): la extensión capturará las pestañas de Instagram abiertas.' % i if i else 'Ya había una orden pendiente.')


def cmd_dia(a):
    """Publicaciones de una cuenta de Instagram de un día o grupo de días. Las miniaturas del perfil no traen fecha:
    con --leer se abren una a una (de la más reciente hacia atrás) hasta pasar de la fecha pedida."""
    rangos = fb.rangos_de(a)
    h = handle_de(a.cuenta)
    pest = a.pestana
    clave = 'ig-' + h.lower() + ('~reels' if pest == 'reels' else '~tagged' if pest == 'etiquetadas' else '')
    url = url_cuenta(h, pest)
    hl = h.lower()

    def propio(p):
        return pest == 'etiquetadas' or hl in (p['autor_url'] or '').lower() or hl in (p['autor'] or '').lower().lstrip('@') \
            or ('/' + hl + '/') in (p['permalink'] or '').lower()

    def codigo(p):
        m = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', p['permalink'] or '')
        return m.group(1) if m else 'id%d' % p['id']
    if a.leer:
        d_min = min(d1 for d1, _ in rangos)
        n = max(1, a.empezar)
        vistas, mas_viejas, abiertas = set(), 0, 0
        hecho = False
        while not hecho:
            print('--- Leyendo la cuadrícula del perfil (hasta %d publicaciones)...' % n)
            estado, det, cid = ejecutar(url, 'capturar', None, -(-n // 6) + 2, '@%s (%s)' % (h, pest), a)
            if estado != 'hecha':
                print('    No se pudo leer (%s).' % (det or estado))
                break
            con = fb.db()
            f = con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()
            ps = con.execute('SELECT * FROM publicaciones WHERE fuente_id=? ORDER BY captura_id DESC, orden ASC', (f['id'],)).fetchall() if f else []
            hubo_nuevas = False
            for p in ps:
                k = codigo(p)
                if k in vistas:
                    continue
                vistas.add(k)
                hubo_nuevas = True
                d = fb.fecha_de_post(con, p)
                if d is None and p['permalink'] and abiertas < a.max_abrir:
                    con.close()
                    abiertas += 1
                    print('    Abriendo publicación %d (%s) para saber su fecha...' % (abiertas, k))
                    ejecutar(p['permalink'], 'capturar', None, 2, 'publicación ' + k, a)
                    con = fb.db()
                    d = fb.fecha_de_post(con, p)
                if d is not None and d < d_min:
                    mas_viejas += 1
                    if mas_viejas >= 2:   # dos seguidas más antiguas: no es una publicación fijada, ya se pasó de la fecha
                        print('    Ya se llegó a antes del %s.' % d_min)
                        hecho = True
                        break
                elif d is not None:
                    mas_viejas = 0
            con.close()
            if hecho:
                break
            if not hubo_nuevas:
                print('    No carga más publicaciones.')
                break
            if abiertas >= a.max_abrir:
                print('    Tope de %d publicaciones abiertas alcanzado (--max-abrir).' % a.max_abrir)
                break
            if n >= a.max:
                print('    Tope de %d publicaciones alcanzado (--max).' % a.max)
                break
            n = min(a.max, n * 2)
    con = fb.db()
    f = con.execute('SELECT * FROM fuentes WHERE clave=?', (clave,)).fetchone()
    mostrar_perfil(con, h)
    if not f:
        print('Todavía no hay publicaciones guardadas de @%s. Añade --leer para leerla ahora.' % h)
        return
    bajar_imagenes(a, f['id'])
    fb.mostrar_dia(con, None, rangos, a, '@' + h + ' (' + pest + ')', filtro=propio, com=True, aprox=False, leido=a.leer, unico=codigo)
    print('\nInstagram solo da la fecha de una publicación al abrirla: las que no se abrieron no salen aquí.')
    print('Para abrir más:  --max-abrir 80   (cada una tarda ~15 s).')


def cmd_lugar(a):
    """Seguir un pueblo o ciudad en Instagram: cuentas, etiquetas (#laadrada) y búsquedas de su nombre."""
    k, d = fb.lugar_actualizar_def(a, {'cuenta': 'cuentas', 'etiqueta': 'etiquetas', 'busca': 'busquedas'})
    if d is None:
        return
    cuentas, tags, busq = list(d.get('cuentas', [])), list(d.get('etiquetas', [])), list(d.get('busquedas', []))
    if not (cuentas or tags or busq):
        sys.exit('El lugar "%s" no tiene nada que seguir. Añade --cuenta usuario, --etiqueta laadrada o --busca "texto".' % k)
    print('LUGAR: %s   (%d cuentas, %d etiquetas, %d búsquedas)' % (k, len(cuentas), len(tags), len(busq)))
    if a.solo_guardar:
        print('Guardado (no se lee nada ahora). Para leerlo: python ig.py lugar "%s"' % k)
        return
    comun = dict(imagenes=a.imagenes, no_marcar=a.no_marcar, espera=a.espera, navegador=a.navegador, scroll=a.scroll,
                 elegir=None, no_preguntar=True)
    cab = lambda t: print('\n' + '#' * fb.ancho() + '\n' + t + '\n' + '#' * fb.ancho())
    if not a.sin_cuentas:
        for c in cuentas:
            cab(c)
            try:
                cmd_cuenta(argparse.Namespace(cuenta=c, pestana='publicaciones', n=a.n, guardado=False, solo_nuevas=False, **comun))
            except SystemExit as e:
                print('   (no terminó: %s)' % e)
    if not a.sin_etiquetas:
        for t in tags:
            cab('#' + t.lstrip('#'))
            try:
                cmd_etiqueta(argparse.Namespace(etiqueta=t, n=a.n, **comun))
            except SystemExit as e:
                print('   (no terminó: %s)' % e)
    if not a.sin_buscar:
        for t in busq:
            cab('Búsqueda: ' + t)
            try:
                cmd_buscar(argparse.Namespace(consulta=[t], exacta=False, n=a.n, leer=None, pestana='publicaciones', **comun))
            except SystemExit as e:
                print('   (no terminó: %s)' % e)
    print('\nUn día concreto de una cuenta:  python ig.py dia usuario --dia ayer --leer')


ICMDS = {
    'servidor': cmd_servidor, 'cuenta': cmd_cuenta, 'hilo': cmd_hilo, 'inicio': cmd_inicio, 'etiqueta': cmd_etiqueta,
    'siguiendo': cmd_siguiendo, 'seguidores': cmd_seguidores, 'cuentas': cmd_cuentas, 'seguir': cmd_seguir, 'dejar': cmd_dejar,
    'buscar': cmd_buscar, 'sugerir': cmd_sugerir, 'elegir': cmd_elegir, 'resultados': cmd_resultados, 'actividad': cmd_actividad,
    'mensajes': cmd_mensajes, 'abiertas': cmd_abiertas,
    'dia': cmd_dia, 'lugar': cmd_lugar,
}

AYUDA = """Instagram Vigía %s (ig.py, usa fb.py %s) - Instagram desde Termux

PREPARACIÓN
  python ig.py servidor                        Receptor para Instagram (puerto 8783). Déjalo abierto, o no hace falta:
                                               las órdenes de abajo lo arrancan solas si no está.
  Firefox con la extensión FB Vigía 1.5 y la sesión de Instagram iniciada.

BUSCAR (como el cuadro «Buscar» de Instagram)
  python ig.py sugerir ciberseguridad          La lista automática que sale al escribir (cuentas, etiquetas, lugares).
  python ig.py buscar "una frase"              Frase (entre comillas).
  python ig.py buscar palabra otra             Palabras sueltas.      --scroll N: más resultados.
     Muestra los resultados numerados y pregunta cuáles abrir (1   1,3   2-4   todos):
     una cuenta se abre con su perfil y sus publicaciones; una etiqueta, con sus publicaciones.
  python ig.py resultados [Nº] | --listar      Volver a ver una búsqueda.
  python ig.py elegir 1,3 [-b Nº]              Abrir resultados de una búsqueda ya hecha.
  python ig.py etiqueta ciberseguridad         Publicaciones de un #hashtag.

CUENTAS, PUBLICACIONES Y COMENTARIOS
  python ig.py cuenta @usuario|Nº [--pestana publicaciones|reels|etiquetadas] [-n 12] [--elegir 1,3|todos]
     Perfil y lista numerada de sus publicaciones; al elegir, se lee cada una completa (texto, fecha, imágenes, comentarios).
  python ig.py hilo URL|código|Nº [--imagenes] [-e 1,2]    Una publicación con sus comentarios.
  python ig.py inicio [--siguiendo] [-n 20]    Tu inicio («Para ti» o «Siguiendo»).
  python ig.py siguiendo [@tuusuario]          A quién sigues (se guarda); luego: python ig.py cuenta Nº
  python ig.py seguidores [@tuusuario]         Quién te sigue.
  python ig.py cuentas [--sigo] [--me-sigue] [texto]   Cuentas guardadas.
  python ig.py seguir @usuario|Nº              Seguir una cuenta.
  python ig.py dejar  @usuario|Nº              Dejar de seguirla.

TU ACTIVIDAD Y MENSAJES
  python ig.py actividad megusta|comentarios|republicaciones|historias|opiniones [-n 20]
  python ig.py mensajes --actualizar [--conversaciones 5] [--nuevos]   Mensajes directos (conversaciones sin probar).

UN DÍA CONCRETO Y SEGUIR UN PUEBLO
  python ig.py dia usuario --dia ayer --leer      Publicaciones de esa cuenta de un día (abre cada una para leer su fecha)
  python ig.py lugar "La Adrada" --cuenta usuario --etiqueta laadrada   Seguir un pueblo (cuentas, etiquetas, búsquedas)

LEER LO GUARDADO (igual que fb.py)
  python ig.py nuevas | ultimas -n 20 | ver FUENTE | post Nº | fuentes | exportar FUENTE | imagenes | enlaces FUENTE
  python ig.py lanzar                          Abre y captura todas las cuentas guardadas como vigiladas.
  python ig.py local "texto"                   Busca en lo ya guardado (no en Instagram).
  python ig.py estado | cola | diagnostico | abiertas

Opciones comunes: --imagenes (descargar las imágenes y mostrar su ruta), -e 1,2 (enlaces a perfiles: 1=quien
publica, 2=quienes comentan), --scroll N, --espera S, --navegador PAQUETE.
Datos en: %s
"""


class _Reescribe(object):
    """Los textos de fb.py hablan de «python fb.py ...»: aquí se muestran como «python ig.py ...»."""
    def __init__(self, f):
        self._f = f

    def write(self, t):
        return self._f.write(t.replace('python fb.py', 'python ig.py').replace('fb.py post', 'ig.py post')
                             .replace('FB Vigía %s' % fb.VERSION, 'Instagram Vigía %s (fb.py %s)' % (IVERSION, fb.VERSION))
                             .replace('Las URL de imágenes de Facebook caducan', 'Las URL de imágenes pueden caducar')
                             .replace('(o x.py diagnostico)', '')
                             .replace('Abre una conversación en facebook.com/messages/t/... y captúrala con la extensión.', 'Pídelas con:  python ig.py mensajes --actualizar --conversaciones 5'))

    def __getattr__(self, k):
        return getattr(self._f, k)


def main():
    fb.proteger_terminal()      # que la terminal de Termux no se quede sin eco (fb.py 1.46)
    sys.stdout = _Reescribe(sys.stdout)
    sys.stderr = _Reescribe(sys.stderr)
    if len(sys.argv) < 2 or sys.argv[1] in ('ayuda', 'help', '-h', '--help'):
        print(AYUDA % (IVERSION, fb.VERSION, fb.DIR))
        return
    cmd = sys.argv[1]
    if cmd not in ICMDS:
        # el resto de órdenes son las de fb.py, sobre los datos de Instagram
        if cmd == 'local':
            sys.argv[1] = 'buscar'
        elif cmd == 'enlaces-seguir':
            sys.argv[1] = 'seguir'
        fb.config()
        return fb.main()
    ap = argparse.ArgumentParser(prog='ig.py ' + cmd)
    ap.add_argument('--espera', type=int, default=240)
    ap.add_argument('--navegador')
    ap.add_argument('--scroll', type=int)
    ap.add_argument('--imagenes', action='store_true', help='descargar las imágenes y mostrar su ruta')
    ap.add_argument('-e', '--enlaces', metavar='LISTA', help='enlaces a perfiles: 1=quien publica 2=quienes comentan')
    ap.add_argument('--no-marcar', action='store_true')
    if cmd == 'servidor':
        ap.add_argument('--puerto', type=int)
    elif cmd == 'cuenta':
        ap.add_argument('cuenta')
        ap.add_argument('--pestana', '--pestaña', default='publicaciones', choices=['publicaciones', 'reels', 'etiquetadas'])
        ap.add_argument('-n', type=int, default=12)
        ap.add_argument('--guardado', action='store_true', help='no abrir Instagram: mostrar lo ya guardado')
        ap.add_argument('--solo-nuevas', action='store_true')
        ap.add_argument('--elegir', '--leer', dest='elegir', help='qué publicaciones leer completas: 1,3 | 2-4 | todos')
        ap.add_argument('--no-preguntar', action='store_true')
    elif cmd == 'hilo':
        ap.add_argument('publicacion')
    elif cmd == 'etiqueta':
        ap.add_argument('etiqueta')
        ap.add_argument('-n', type=int, default=0)
        ap.add_argument('--elegir', '--leer', dest='elegir')
        ap.add_argument('--no-preguntar', action='store_true')
    elif cmd == 'inicio':
        ap.add_argument('-n', type=int, default=20)
        ap.add_argument('--siguiendo', action='store_true')
        ap.add_argument('--solo-nuevas', action='store_true')
    elif cmd in ('siguiendo', 'seguidores'):
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
        ap.add_argument('--exacta', action='store_true')
        ap.add_argument('--elegir')
        ap.add_argument('--leer', help='al abrir una cuenta o etiqueta: qué publicaciones leer completas (1,3 | todos)')
        ap.add_argument('--no-preguntar', action='store_true')
        ap.add_argument('-n', type=int, default=0, help='cuántos resultados mostrar / publicaciones por cuenta abierta')
        ap.add_argument('--pestana', default='publicaciones')
    elif cmd == 'elegir':
        ap.add_argument('seleccion')
        ap.add_argument('-b', '--busqueda', type=int)
        ap.add_argument('-n', type=int, default=12)
        ap.add_argument('--leer')
        ap.add_argument('--pestana', default='publicaciones')
    elif cmd == 'resultados':
        ap.add_argument('busqueda', nargs='?', type=int)
        ap.add_argument('--listar', action='store_true')
        ap.add_argument('-n', type=int, default=0)
    elif cmd == 'sugerir':
        ap.add_argument('termino', nargs='+')
        ap.add_argument('--elegir')
        ap.add_argument('--leer')
        ap.add_argument('--no-preguntar', action='store_true')
        ap.add_argument('-n', type=int, default=12)
    elif cmd == 'actividad':
        ap.add_argument('tipo', nargs='?', default='megusta')
        ap.add_argument('-n', type=int, default=20)
        ap.add_argument('--solo-nuevas', action='store_true')
    elif cmd == 'dia':
        ap.add_argument('cuenta')
        fb.opciones_dia(ap)
        ap.add_argument('--pestana', '--pestaña', default='publicaciones', choices=['publicaciones', 'reels', 'etiquetadas'])
        ap.add_argument('--leer', action='store_true', help='leer la cuenta hacia atrás (abriendo publicaciones) hasta llegar a esa fecha')
        ap.add_argument('--empezar', type=int, default=12, help='con --leer: publicaciones de la primera lectura (12); si no llega se duplica')
        ap.add_argument('--max', type=int, default=120, help='con --leer: tope de publicaciones de la cuadrícula (120)')
        ap.add_argument('--max-abrir', type=int, default=40, help='con --leer: tope de publicaciones que se abren una a una para leer su fecha (40)')
    elif cmd == 'lugar':
        ap.add_argument('nombre')
        ap.add_argument('--cuenta', action='append', help='cuenta a seguir (usuario); se puede repetir')
        ap.add_argument('--etiqueta', action='append', help='etiqueta a seguir (laadrada o #laadrada); se puede repetir')
        ap.add_argument('--busca', action='append', help='texto a buscar en Instagram; se puede repetir')
        ap.add_argument('--quitar', action='store_true', help='quitar lo indicado (sin nada más: borrar el lugar)')
        ap.add_argument('-n', type=int, default=12, help='publicaciones a mostrar de cada cuenta/etiqueta (12)')
        ap.add_argument('--solo-guardar', action='store_true')
        ap.add_argument('--sin-cuentas', action='store_true')
        ap.add_argument('--sin-etiquetas', action='store_true')
        ap.add_argument('--sin-buscar', action='store_true')
    elif cmd == 'mensajes':
        ap.add_argument('conv', nargs='?')
        ap.add_argument('--actualizar', action='store_true')
        ap.add_argument('--conversaciones', type=int, default=0)
        ap.add_argument('--nuevos', action='store_true')
    a = ap.parse_args(sys.argv[2:])
    fb.config()
    fb.aplicar_opciones(a)
    ICMDS[cmd](a)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        pass
