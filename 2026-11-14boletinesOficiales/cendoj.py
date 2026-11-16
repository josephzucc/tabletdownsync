#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cendoj.py - Buscador de jurisprudencia del CENDOJ (poderjudicial.es) desde Termux.
Trabaja junto con la extension de Firefox "CENDOJ Puente" (cendoj.xpi).

  python cendoj.py buscar "covid persistente incapacidad absoluta" -j social -o tsj -m 30 --pdf
  python cendoj.py roj "STSJ CL 3456/2024" --pdf
  python cendoj.py local "trastorno adaptativo"
  python cendoj.py ayuda

Texto normal -> salida estandar (stdout). Errores del programa -> stderr.
Manual completo: cendojManualBono.txt
"""
import argparse, base64, datetime as dt, glob, json, os, re, shlex, shutil, subprocess, sys, tempfile
import textwrap, threading, time, traceback, unicodedata, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.1'
DIR = os.path.expanduser(os.environ.get('CENDOJ_DIR', '~/cendoj'))
CFG_F = os.path.join(DIR, 'config.json')
CFG_DEF = {
    'puerto': 8779,
    'paquete_firefox': 'org.mozilla.fenix',   # Firefox Nightly; Firefox normal = org.mozilla.firefox
    'ancho': 121,
    'espera_extension': 15,                   # s esperando a la extension antes de abrir Firefox
    'espera_total': 900,                      # s maximos por trabajo
    'pdf_directo': False,                     # True = intentar bajar el PDF desde Termux (el CENDOJ suele pedir captcha)
    'pdftotext_layout': False,                # True = conservar la maquetacion del PDF (columnas, sangrias)
    'ocr': True,                              # si el PDF no trae texto: OCR con tesseract (pdftoppm + tesseract -l spa)
    'ocr_idioma': 'spa',
}
URL_BUSCADOR = 'https://www.poderjudicial.es/search/indexAN.jsp'
UA = ('Mozilla/5.0 (Android 14; Mobile; rv:144.0) Gecko/144.0 Firefox/144.0')

JURIS = {'social': 'SOCIAL', 'civil': 'CIVIL', 'penal': 'PENAL', 'contencioso': 'CONTENCIOSO',
         'contencioso-administrativo': 'CONTENCIOSO', 'cont': 'CONTENCIOSO', 'militar': 'MILITAR',
         'especial': 'ESPECIAL'}
ORGANOS = {'ts': 'Tribunal Supremo', 'supremo': 'Tribunal Supremo',
           'tsj': 'Tribunal Superior de Justicia', 'an': 'Audiencia Nacional',
           'ap': 'Audiencia Provincial', 'js': 'Juzgado de lo Social',
           'jcont': 'Juzgado de lo Contencioso', 'ti': 'Tribunal de Instancia',
           'tc': 'Tribunal Constitucional'}


# ------------------------------------------------------------------ utilidades
def out(s=''):
    print(s, flush=True)


def err(s):
    print('ERROR: ' + s, file=sys.stderr, flush=True)


def cfg():
    os.makedirs(DIR, exist_ok=True)
    c = dict(CFG_DEF)
    if os.path.exists(CFG_F):
        try:
            c.update(json.load(open(CFG_F, encoding='utf-8')))
        except Exception as e:
            err('config.json no es valido (%s); uso valores por defecto' % e)
    else:
        json.dump(CFG_DEF, open(CFG_F, 'w', encoding='utf-8'), indent=2, ensure_ascii=False)
    return c


C = None


def W():
    return int(C.get('ancho', 121))


def wrap(texto, ind='', ind2=None):
    if ind2 is None:
        ind2 = ind
    return textwrap.fill(texto, W(), initial_indent=ind, subsequent_indent=ind2,
                         break_long_words=True, break_on_hyphens=False) if texto else ind.rstrip()


def slug(s, n=40):
    s = unicodedata.normalize('NFD', s or '').encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9]+', '_', s).strip('_')
    return (s[:n] or 'busqueda').lower()


def nombre_doc(r, i):
    base = r.get('roj') or r.get('ecli') or ('ref_' + str(r.get('ref') or i))
    return re.sub(r'[^A-Za-z0-9]+', '_', base).strip('_')


def fecha_txt(f):
    f = str(f or '')
    return '%s/%s/%s' % (f[6:8], f[4:6], f[0:4]) if re.fullmatch(r'\d{8}', f) else f


def rewrap_largas(texto):
    res = []
    for linea in texto.splitlines():
        linea = linea.rstrip()
        if len(linea) <= W():
            res.append(linea)
            continue
        ind = re.match(r'\s*', linea).group(0)[:20]
        res.extend(textwrap.wrap(linea.strip(), W(), initial_indent=ind, subsequent_indent=ind,
                                 break_long_words=True) or [''])
    return '\n'.join(res) + '\n'


# ------------------------------------------------------------------ diagnostico
class Diag:
    def __init__(self, comando):
        d = os.path.join(DIR, 'diag')
        os.makedirs(d, exist_ok=True)
        self.f = os.path.join(d, dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '_' + comando + '.txt')
        self.lineas = []
        self.add('cendoj.py v%s  python %s  plataforma %s' % (VERSION, sys.version.split()[0], sys.platform))
        self.add('argumentos: ' + json.dumps(sys.argv[1:], ensure_ascii=False))
        self.add('config: ' + json.dumps(C, ensure_ascii=False))
        self.add('pdftotext: %s | pdftoppm: %s | tesseract: %s | pypdf: %s | am: %s | termux-open-url: %s' % (
            shutil.which('pdftotext'), shutil.which('pdftoppm'), shutil.which('tesseract'), _hay('pypdf'),
            shutil.which('am'), shutil.which('termux-open-url')))

    def add(self, m):
        self.lineas.append(dt.datetime.now().strftime('%H:%M:%S.%f')[:-3] + ' ' + m)
        self.guardar()

    def guardar(self):
        try:
            with open(self.f, 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(self.lineas) + '\n')
            with open(os.path.join(DIR, 'diag', 'ultimo.txt'), 'w', encoding='utf-8') as fh:
                fh.write(self.f + '\n')
        except Exception:
            pass


def _hay(mod):
    try:
        __import__(mod)
        return 'si'
    except Exception:
        return 'no'


DIAG = None


# ------------------------------------------------------------------ servidor local
class Estado:
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.entregado = False
        self.ultimo_sondeo = 0
        self.version_ext = ''
        self.items = []
        self.paginas = []
        self.pdfs = []
        self.fin = None
        self.ev = threading.Event()


E = Estado()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _resp(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path.startswith('/cmd'):
            m = re.search(r'v=([\d.]+)', self.path)
            with E.lock:
                E.ultimo_sondeo = time.time()
                if m:
                    E.version_ext = m.group(1)
                if E.job and not E.entregado:
                    E.entregado = True
                    DIAG.add('extension v%s recoge el trabajo %s' % (E.version_ext, E.job['jobid']))
                    return self._resp(E.job)
            return self._resp({})
        if self.path.startswith('/ping'):
            return self._resp({'cendoj': VERSION})
        self._resp({'error': 'ruta'}, 404)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            d = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception as e:
            DIAG.add('POST %s con JSON invalido: %s' % (self.path, e))
            return self._resp({'ok': False})
        jid = (E.job or {}).get('jobid')
        if d.get('jobid') and jid and d.get('jobid') != jid:
            DIAG.add('mensaje de otro trabajo ignorado: ' + str(d.get('jobid')))
            return self._resp({'ok': True})
        if self.path == '/log':
            DIAG.add('EXT ' + str(d.get('m', ''))[:4000])
        elif self.path == '/pagina':
            its = d.get('items') or []
            with E.lock:
                E.items.extend(its)
                E.paginas.append(d.get('info') or {})
            out('  ... pagina %s: %d resultados (llevo %d)' % (d.get('pagina'), len(its), len(E.items)))
            DIAG.add('pagina %s recibida: %d items, info=%s' % (d.get('pagina'), len(its),
                                                                json.dumps(d.get('info'), ensure_ascii=False)))
        elif self.path == '/pdf':
            with E.lock:
                E.pdfs.append(d)
            DIAG.add('pdf %s ok=%s ct=%s bytes_b64=%d %s' % (d.get('nombre'), d.get('ok'), d.get('ct'),
                                                             len(d.get('b64') or ''), d.get('error', '')))
            if not d.get('ok') and d.get('html'):
                DIAG.add('HTML recibido en lugar de PDF (%s):\n%s' % (d.get('url'), d['html'][:20000]))
        elif self.path == '/aviso':
            DIAG.add('AVISO de la extension: %s %s' % (d.get('m'), d.get('url')))
            if d.get('m') == 'CAPTCHA':
                out('')
                out('*' * min(W(), 80))
                out('  EL CENDOJ PIDE UN CAPTCHA ("Control Descargas masivas").')
                out('  Ve a Firefox: escribe el texto de la imagen y pulsa el boton de enviar.')
                out('  Tienes 10 minutos. En cuanto lo resuelvas, sigo yo solo.')
                out('*' * min(W(), 80))
                out('')
        elif self.path == '/fin':
            DIAG.add('FIN ok=%s error=%s res=%s url=%s' % (d.get('ok'), d.get('error'),
                                                          json.dumps(d.get('res'), ensure_ascii=False), d.get('url')))
            if d.get('dom'):
                DIAG.add('VOLCADO DOM:\n' + d['dom'])
            with E.lock:
                E.fin = d
            E.ev.set()
        self._resp({'ok': True})


SERVIDOR = None
PARSER_BUSCAR = None


def arrancar_servidor():
    global SERVIDOR
    if SERVIDOR:
        return
    p = int(C['puerto'])
    try:
        SERVIDOR = ThreadingHTTPServer(('127.0.0.1', p), H)
    except OSError as e:
        err('no puedo abrir el puerto %d (%s). Puede que otro cendoj.py siga abierto: cierralo o '
            'cambia "puerto" en %s (y en background.js de la extension).' % (p, e, CFG_F))
        sys.exit(2)
    threading.Thread(target=SERVIDOR.serve_forever, daemon=True).start()
    DIAG.add('servidor en 127.0.0.1:%d' % p)


def despertar_firefox(url=URL_BUSCADOR):
    pkg = C.get('paquete_firefox') or 'org.mozilla.fenix'
    cmds = []
    if shutil.which('am'):
        cmds.append(['am', 'start', '-a', 'android.intent.action.VIEW', '-d', url, '-p', pkg])
    if shutil.which('termux-open-url'):
        cmds.append(['termux-open-url', url])
    for c in cmds:
        try:
            r = subprocess.run(c, capture_output=True, text=True, timeout=20)
            DIAG.add('despertar: %s -> rc=%s %s %s' % (' '.join(c), r.returncode, r.stdout.strip(), r.stderr.strip()))
            if r.returncode == 0 and 'Error' not in (r.stdout + r.stderr):
                return True
        except Exception as e:
            DIAG.add('despertar fallo %s: %s' % (c, e))
    return False


def trabajo(tipo, **campos):
    """Encola un trabajo, espera a la extension y devuelve el mensaje final."""
    jid = '%s-%d' % (tipo, int(time.time() * 1000))
    job = dict(campos, tipo=tipo, jobid=jid, timeout=int(C['espera_total']))
    with E.lock:
        E.job, E.entregado, E.fin = job, False, None
        E.items, E.paginas, E.pdfs = [], [], []
    E.ev.clear()
    DIAG.add('trabajo encolado: ' + json.dumps(job, ensure_ascii=False))
    t0 = time.time()
    despierto = False
    while True:
        if E.ev.wait(1):
            break
        transcurrido = time.time() - t0
        if not E.entregado:
            if not despierto and transcurrido > float(C['espera_extension']):
                out('  La extension no responde: abro Firefox en el buscador del CENDOJ...')
                despierto = True
                if not despertar_firefox():
                    err('no he podido abrir Firefox desde Termux (ver diagnostico).')
            if transcurrido > float(C['espera_extension']) + 90:
                DIAG.add('la extension no recogio el trabajo')
                err('la extension "CENDOJ Puente" no ha contestado. Comprueba que esta instalada y activada, '
                    'y que Firefox esta abierto. Diagnostico: ' + DIAG.f)
                return None
        if transcurrido > float(C['espera_total']) + 30:
            err('tiempo agotado esperando el final del trabajo. Diagnostico: ' + DIAG.f)
            return None
    return E.fin


# ------------------------------------------------------------------ PDF y TXT
def _ocr(pdf):
    """OCR con las herramientas ya instaladas en Termux: pdftoppm (poppler) + tesseract."""
    if not (C.get('ocr', True) and shutil.which('pdftoppm') and shutil.which('tesseract')):
        return None
    tmp = tempfile.mkdtemp(prefix='cendoj_ocr_')
    try:
        r = subprocess.run(['pdftoppm', '-r', '300', '-gray', '-png', pdf, os.path.join(tmp, 'p')],
                           capture_output=True, timeout=900)
        if r.returncode != 0:
            DIAG.add('pdftoppm fallo: ' + r.stderr.decode('utf-8', 'replace')[:500])
            return None
        paginas = sorted(glob.glob(os.path.join(tmp, 'p*.png')))
        out('     OCR de %d paginas con tesseract (puede tardar)...' % len(paginas))
        trozos = []
        for img in paginas:
            t = subprocess.run(['tesseract', img, '-', '-l', C.get('ocr_idioma', 'spa')],
                               capture_output=True, timeout=600)
            if t.returncode != 0:
                DIAG.add('tesseract fallo en %s: %s' % (img, t.stderr.decode('utf-8', 'replace')[:500]))
                if b'spa' in t.stderr or b'traineddata' in t.stderr:
                    err('tesseract no tiene el idioma "%s" (ver manual, apartado OCR)' % C.get('ocr_idioma', 'spa'))
                    return None
            trozos.append(t.stdout.decode('utf-8', 'replace'))
        return '\n\n'.join(trozos)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def pdf_a_txt(pdf, txt):
    texto, metodo = None, ''
    if shutil.which('pdftotext'):
        try:
            cmd = ['pdftotext'] + (['-layout'] if C.get('pdftotext_layout') else []) + ['-enc', 'UTF-8', pdf, '-']
            r = subprocess.run(cmd, capture_output=True, timeout=180)
            if r.returncode == 0 and r.stdout.strip():
                texto, metodo = r.stdout.decode('utf-8', 'replace'), 'pdftotext'
        except Exception as e:
            DIAG.add('pdftotext fallo en %s: %s' % (pdf, e))
    if texto is None:
        try:
            import pypdf
            rd = pypdf.PdfReader(pdf)
            texto = '\n\n'.join((p.extract_text() or '') for p in rd.pages)
            metodo = 'pypdf'
        except Exception as e:
            DIAG.add('pypdf fallo en %s: %s' % (pdf, e))
    if not texto or len(texto.strip()) < 200:
        t2 = _ocr(pdf)
        if t2 and len(t2.strip()) > len((texto or '').strip()):
            texto, metodo = t2, 'OCR tesseract'
    if not texto or not texto.strip():
        DIAG.add('sin texto extraible en ' + pdf)
        return False, 'sin texto (instala poppler: pkg install poppler; para PDF escaneados: pkg install tesseract)'
    texto = texto.replace('\f', '\n')
    with open(txt, 'w', encoding='utf-8') as fh:
        fh.write(rewrap_largas(texto))
    return True, metodo


def bajar_directo(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': 'application/pdf,*/*'})
        with urllib.request.urlopen(req, timeout=60) as r:
            b = r.read()
            DIAG.add('descarga directa %s -> %s %s %d bytes' % (url, r.status, r.headers.get('Content-Type'), len(b)))
            return b if b[:4] == b'%PDF' else None
    except Exception as e:
        DIAG.add('descarga directa fallo %s: %s' % (url, e))
        return None


def descargar(carpeta, resultados, indices, convertir=True):
    dp, dtx = os.path.join(carpeta, 'pdf'), os.path.join(carpeta, 'txt')
    os.makedirs(dp, exist_ok=True)
    os.makedirs(dtx, exist_ok=True)
    pend = []
    for i in indices:
        r = resultados[i - 1]
        if not r.get('url'):
            out('  [%d] sin enlace al documento' % i)
            continue
        nom = nombre_doc(r, i)
        r['pdf'] = 'pdf/%s.pdf' % nom
        destino = os.path.join(carpeta, r['pdf'])
        if os.path.exists(destino) and os.path.getsize(destino) > 0:
            out('  [%d] %s ya estaba descargado' % (i, nom))
        else:
            b = bajar_directo(r['url']) if C.get('pdf_directo', True) else None
            if b:
                open(destino, 'wb').write(b)
                out('  [%d] %s descargado (%d KB)' % (i, nom, len(b) // 1024))
            else:
                pend.append({'nombre': nom, 'url': r['url'], 'i': i})
    if pend:
        out('  Pido a la extension %d PDF que no se pudieron bajar directamente...' % len(pend))
        fin = trabajo('pdf', docs=[{'nombre': p['nombre'], 'url': p['url']} for p in pend])
        recibidos = {p.get('nombre'): p for p in E.pdfs}
        for p in pend:
            d = recibidos.get(p['nombre'])
            destino = os.path.join(dp, p['nombre'] + '.pdf')
            if d and d.get('ok') and d.get('b64'):
                b = base64.b64decode(d['b64'])
                open(destino, 'wb').write(b)
                out('  [%d] %s descargado por la extension (%d KB)' % (p['i'], p['nombre'], len(b) // 1024))
            else:
                resultados[p['i'] - 1].pop('pdf', None)
                err('[%d] no se pudo descargar %s (ver diagnostico %s)' % (p['i'], p['nombre'], DIAG.f))
        if fin is None:
            err('la extension no termino la descarga de PDF')
    if convertir:
        for i in indices:
            r = resultados[i - 1]
            if not r.get('pdf'):
                continue
            pdf = os.path.join(carpeta, r['pdf'])
            if not os.path.exists(pdf):
                continue
            t = 'txt/%s.txt' % os.path.splitext(os.path.basename(pdf))[0]
            ok, metodo = pdf_a_txt(pdf, os.path.join(carpeta, t))
            if ok:
                r['txt'] = t
                out('  [%d] convertido a %s (%s)' % (i, t, metodo))
            else:
                err('[%d] no se pudo convertir %s: %s' % (i, r['pdf'], metodo))


# ------------------------------------------------------------------ guardar / mostrar
def escribir_resultados(carpeta, consulta, resultados, extra):
    json.dump({'consulta': consulta, 'resultados': resultados, 'extra': extra},
              open(os.path.join(carpeta, 'resultados.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    L = []
    L.append('=' * W())
    L.append(wrap('CENDOJ - ' + (consulta.get('descripcion') or '')))
    L.append(wrap('Filtros: ' + json.dumps(consulta.get('filtros'), ensure_ascii=False)))
    L.append('Fecha de la consulta: %s   Resultados guardados: %d' % (consulta.get('fecha'), len(resultados)))
    if extra.get('total_web'):
        L.append(wrap('Segun la web: ' + extra['total_web']))
    L.append('=' * W())
    for i, r in enumerate(resultados, 1):
        L.append('')
        L.append(wrap('[%d] %s' % (i, r.get('titulo') or r.get('roj') or r.get('ref')), '', '     '))
        partes = [x for x in [r.get('ecli'), r.get('sala')] if x]
        for k, v in (r.get('datos') or {}).items():
            partes.append('%s: %s' % (k, v))
        if r.get('fecha'):
            partes.append('Fecha: ' + fecha_txt(r['fecha']))
        L.append(wrap(' | '.join(partes), '     ', '     '))
        L.append(wrap('URL: ' + (r.get('url') or ''), '     ', '     '))
        if r.get('pdf') or r.get('txt'):
            L.append(wrap('PDF: %s   TXT: %s' % (r.get('pdf', '-'), r.get('txt', '-')), '     ', '     '))
        if r.get('resumen'):
            L.append(wrap('RESUMEN: ' + r['resumen'], '     ', '     '))
    L.append('')
    with open(os.path.join(carpeta, 'resultados.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(L) + '\n')
    with open(os.path.join(DIR, 'ultima.txt'), 'w', encoding='utf-8') as fh:
        fh.write(carpeta + '\n')


def cargar(carpeta):
    if carpeta in (None, '', 'ultima', 'u'):
        f = os.path.join(DIR, 'ultima.txt')
        if not os.path.exists(f):
            err('todavia no hay ninguna busqueda guardada')
            sys.exit(1)
        carpeta = open(f, encoding='utf-8').read().strip()
    elif not os.path.isabs(carpeta) and not os.path.isdir(carpeta):
        cands = sorted(glob.glob(os.path.join(DIR, 'busquedas', '*' + carpeta + '*')))
        if not cands:
            err('no encuentro la busqueda "%s" (usa: cendoj.py lista)' % carpeta)
            sys.exit(1)
        carpeta = cands[-1]
    j = json.load(open(os.path.join(carpeta, 'resultados.json'), encoding='utf-8'))
    return carpeta, j


def indices_de(sel, n):
    if not sel or sel == ['todos']:
        return list(range(1, n + 1))
    res = []
    for s in sel:
        for parte in s.split(','):
            parte = parte.strip()
            if re.fullmatch(r'\d+-\d+', parte):
                a, b = map(int, parte.split('-'))
                res.extend(range(a, b + 1))
            elif parte.isdigit():
                res.append(int(parte))
    return [i for i in res if 1 <= i <= n]


def mostrar_lista(resultados, resumen_corto=True, desde=1):
    for i, r in enumerate(resultados, desde):
        out(wrap('[%d] %s' % (i, r.get('titulo') or r.get('roj')), '', '     '))
        info = [x for x in [r.get('sala'), (r.get('datos') or {}).get('Nº Recurso') and
                            'Rec. ' + r['datos']['Nº Recurso'], (r.get('datos') or {}).get('Nº de Resolución') and
                            'Res. ' + r['datos']['Nº de Resolución']] if x]
        if info:
            out(wrap(' | '.join(info), '     '))
        if r.get('resumen'):
            t = r['resumen']
            if resumen_corto and len(t) > 300:
                t = t[:300].rsplit(' ', 1)[0] + ' (...)'
            out(wrap(t, '     '))


# ------------------------------------------------------------------ comandos
def filtros_de(a):
    f = {}
    if getattr(a, 'texto', None):
        f['texto'] = ' '.join(a.texto)
    for k in ('roj', 'ecli', 'numres', 'recurso', 'ponente', 'seccion', 'desde', 'hasta'):
        v = getattr(a, k, None)
        if v:
            f[k] = v
    if getattr(a, 'jurisdiccion', None):
        f['jurisdiccion'] = [JURIS.get(x.strip().lower(), x.strip().upper()) for x in a.jurisdiccion.split(',')]
    if getattr(a, 'organo', None):
        f['organo'] = [ORGANOS.get(x.strip().lower(), x.strip()) for x in a.organo.split(',')]
    if getattr(a, 'lugar', None):
        f['lugar'] = [x.strip() for x in a.lugar.split(',')]
    if getattr(a, 'tipo', None):
        f['tiporesolucion'] = [x.strip() for x in a.tipo.split(',')]
    if getattr(a, 'discapacidad', False):
        f['discapacidad'] = True
    for k in ('desde', 'hasta'):
        if k in f and not re.fullmatch(r'\d{2}/\d{2}/\d{4}', f[k]):
            err('la fecha "%s" debe ir como dd/mm/aaaa (ej. 01/01/2023)' % f[k])
            sys.exit(1)
    return f


def cmd_buscar(a, descripcion=None):
    f = filtros_de(a)
    if not f:
        err('indica al menos un texto o un filtro (ver: cendoj.py ayuda)')
        sys.exit(1)
    arrancar_servidor()
    desc = descripcion or ('busqueda: ' + (f.get('texto') or f.get('roj') or f.get('ecli') or json.dumps(f)))
    out('Buscando en el CENDOJ (%s) ... maximo %d resultados' % (desc, a.max))
    fin = trabajo('buscar', filtros=f, max=a.max)
    if fin is None:
        sys.exit(3)
    if not fin.get('ok'):
        err('la busqueda fallo: %s. Diagnostico: %s' % (fin.get('error'), DIAG.f))
        if not E.items:
            sys.exit(4)
    res = E.items
    total_web = ''
    if E.paginas:
        p = E.paginas[-1]
        total_web = ' '.join(x for x in [p.get('hits', ''), ('paginas: ' + p['total']) if p.get('total') else ''] if x)
    marca = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    carpeta = os.path.join(DIR, 'busquedas', marca + '_' + slug(a.nombre or f.get('texto') or f.get('roj') or f.get('ecli') or 'busqueda'))
    os.makedirs(carpeta, exist_ok=True)
    consulta = {'descripcion': desc, 'filtros': f, 'fecha': dt.datetime.now().strftime('%d/%m/%Y %H:%M'),
                'aplicados_en_web': (fin.get('res') or {}).get('aplicados')}
    extra = {'total_web': total_web}
    out('')
    if not res:
        out('Sin resultados.')
    else:
        out('%d resultados:' % len(res))
        out('')
        mostrar_lista(res)
    if res and (a.pdf or a.txt):
        out('')
        out('Descargando documentos...')
        idx = list(range(1, min(len(res), a.maxpdf) + 1))
        descargar(carpeta, res, idx, convertir=True)
    escribir_resultados(carpeta, consulta, res, extra)
    out('')
    out(wrap('Guardado en: ' + carpeta))
    out('  resultados.txt (listado completo con resumenes), resultados.json, pdf/ y txt/')
    out(wrap('Diagnostico: ' + DIAG.f))


def cmd_roj(a):
    a.texto = None
    if a.valor.upper().startswith('ECLI:'):
        a.ecli, a.roj = a.valor, None
    else:
        a.roj, a.ecli = a.valor, None
    a.pdf = True if not a.sinpdf else False
    a.txt = a.pdf
    a.max, a.maxpdf, a.nombre = 5, 5, a.valor
    cmd_buscar(a, descripcion='documento ' + a.valor)


def cmd_pdf(a):
    carpeta, j = cargar(a.busqueda)
    res = j['resultados']
    idx = indices_de(a.numeros, len(res))
    if not idx:
        err('no hay numeros validos (ej: cendoj.py pdf ultima 1 3 5-7, o "todos")')
        sys.exit(1)
    arrancar_servidor()
    out('Descargando %d documentos de %s' % (len(idx), carpeta))
    descargar(carpeta, res, idx, convertir=not a.sintxt)
    escribir_resultados(carpeta, j['consulta'], res, j.get('extra', {}))
    out('Hecho. Diagnostico: ' + DIAG.f)


def cmd_txt(a):
    carpeta, j = cargar(a.busqueda)
    n = 0
    for i, r in enumerate(j['resultados'], 1):
        if r.get('pdf') and os.path.exists(os.path.join(carpeta, r['pdf'])):
            t = 'txt/%s.txt' % os.path.splitext(os.path.basename(r['pdf']))[0]
            os.makedirs(os.path.join(carpeta, 'txt'), exist_ok=True)
            ok, m = pdf_a_txt(os.path.join(carpeta, r['pdf']), os.path.join(carpeta, t))
            if ok:
                r['txt'] = t
                n += 1
                out('  [%d] %s (%s)' % (i, t, m))
            else:
                err('[%d] %s' % (i, m))
    escribir_resultados(carpeta, j['consulta'], j['resultados'], j.get('extra', {}))
    out('%d documentos convertidos a txt.' % n)


def cmd_ver(a):
    carpeta, j = cargar(a.busqueda)
    res = j['resultados']
    if a.numero is None:
        out(open(os.path.join(carpeta, 'resultados.txt'), encoding='utf-8').read())
        return
    if not 1 <= a.numero <= len(res):
        err('numero fuera de rango (1-%d)' % len(res))
        sys.exit(1)
    r = res[a.numero - 1]
    if a.completo and r.get('txt') and os.path.exists(os.path.join(carpeta, r['txt'])):
        out(open(os.path.join(carpeta, r['txt']), encoding='utf-8').read())
        return
    mostrar_lista([r], resumen_corto=False, desde=a.numero)
    out(wrap('URL: ' + r.get('url', ''), '     '))
    if r.get('txt'):
        out(wrap('Texto completo: %s  (cendoj.py ver %s %d -c)' % (os.path.join(carpeta, r['txt']),
                                                                   a.busqueda or 'ultima', a.numero), '     '))
    else:
        out('     Sin texto descargado: cendoj.py pdf %s %d' % (a.busqueda or 'ultima', a.numero))


def cmd_lista(a):
    ds = sorted(glob.glob(os.path.join(DIR, 'busquedas', '*')))
    if not ds:
        out('No hay busquedas guardadas en ' + os.path.join(DIR, 'busquedas'))
        return
    for d in ds[-a.ultimas:]:
        try:
            j = json.load(open(os.path.join(d, 'resultados.json'), encoding='utf-8'))
            n = len(j['resultados'])
            npdf = len(glob.glob(os.path.join(d, 'pdf', '*.pdf')))
            out(wrap('%s  (%d resultados, %d pdf)  %s' % (os.path.basename(d), n, npdf, j['consulta'].get('descripcion', '')),
                     '', '    '))
        except Exception:
            out(os.path.basename(d) + '  (sin resultados.json)')


def cmd_local(a):
    patron = ' '.join(a.texto)
    rx = re.compile(re.escape(patron) if not a.regex else patron, re.I)
    base = os.path.join(DIR, 'busquedas')
    ficheros = sorted(glob.glob(os.path.join(base, '*', 'txt', '*.txt')) + glob.glob(os.path.join(base, '*', 'resultados.txt')))
    if a.en:
        ficheros = [f for f in ficheros if a.en in f]
    total = 0
    for f in ficheros:
        lineas = open(f, encoding='utf-8', errors='replace').read().splitlines()
        hits = [i for i, l in enumerate(lineas) if rx.search(l)]
        if not hits:
            continue
        out('=' * min(W(), 80))
        out(wrap(os.path.relpath(f, base) + '  (%d coincidencias)' % len(hits)))
        for i in hits[:a.maximo]:
            ini, fin = max(0, i - a.contexto), min(len(lineas), i + a.contexto + 1)
            out('  --- linea %d' % (i + 1))
            for k in range(ini, fin):
                out(('>> ' if k == i else '   ') + lineas[k][:W() - 3])
        total += len(hits)
    out('')
    out('Total: %d coincidencias en %d ficheros revisados.' % (total, len(ficheros)))


def cmd_lote(a):
    if not os.path.exists(a.fichero):
        err('no existe el fichero ' + a.fichero)
        sys.exit(1)
    lineas = []
    for n, l in enumerate(open(a.fichero, encoding='utf-8'), 1):
        l = l.strip()
        if not l or l.startswith('#'):
            continue
        if l.startswith('buscar '):
            l = l[7:]
        lineas.append((n, l))
    if not lineas:
        err('el fichero no tiene ninguna busqueda (una por linea; las que empiezan por # se ignoran)')
        sys.exit(1)
    out('Lote de %d busquedas: %s' % (len(lineas), a.fichero))
    hechas, fallidas = 0, []
    for k, (n, l) in enumerate(lineas, 1):
        out('')
        out('#' * min(W(), 80))
        out(wrap('Busqueda %d de %d (linea %d): %s' % (k, len(lineas), n, l)))
        out('#' * min(W(), 80))
        try:
            b = PARSER_BUSCAR.parse_args(shlex.split(l))
        except (SystemExit, ValueError):
            err('linea %d no valida: %s' % (n, l))
            fallidas.append(n)
            continue
        try:
            cmd_buscar(b)
            hechas += 1
        except SystemExit:
            fallidas.append(n)
        time.sleep(2)
    out('')
    out('Lote terminado: %d busquedas hechas%s.' % (hechas, (', fallaron las lineas ' + ', '.join(map(str, fallidas)))
                                                       if fallidas else ''))


def cmd_estado(a):
    arrancar_servidor()
    out('Esperando a la extension "CENDOJ Puente" (hasta %ss)...' % C['espera_extension'])
    fin = trabajo('ping')
    if fin and fin.get('ok'):
        out('Extension conectada. Version: %s. Puerto: %s.' % ((fin.get('res') or {}).get('version'), C['puerto']))
    else:
        err('la extension no responde. Diagnostico: ' + DIAG.f)
        sys.exit(3)


def cmd_abrir(a):
    url = a.que
    if not url.startswith('http'):
        carpeta, j = cargar(a.busqueda)
        n = int(url)
        url = j['resultados'][n - 1]['url']
    out('Abriendo ' + url)
    if not despertar_firefox(url):
        err('no he podido abrir Firefox')


def cmd_diag(a):
    f = os.path.join(DIR, 'diag', 'ultimo.txt')
    if not os.path.exists(f):
        out('No hay diagnosticos todavia.')
        return
    ruta = open(f, encoding='utf-8').read().strip()
    out('Ultimo diagnostico: ' + ruta)
    if a.mostrar:
        out(open(ruta, encoding='utf-8', errors='replace').read())


AYUDA = """\
cendoj.py v%s - buscador del CENDOJ desde Termux (con la extension CENDOJ Puente)

  buscar TEXTO [opciones]    Busca en el CENDOJ y guarda los resultados en ~/cendoj/busquedas/
      -j social|civil|penal|contencioso|militar|especial   (varias: -j social,contencioso)
      -o ts|tsj|an|ap|js|ti|"Texto del organo"           Tipo de organo (varios con comas)
      -l "Madrid,Castilla y Leon"                          Localizacion (comunidad)
      -d 01/01/2023  -a 04/11/2026                         Fecha de resolucion desde / hasta
      -r ROJ  -e ECLI  -n NUM_RESOLUCION  --recurso NUM  -p PONENTE  -s SECCION
      --tipo Sentencia                                     Tipo de resolucion
      --discapacidad                                       Solo las marcadas de interes en discapacidad
      -m 30                                                Maximo de resultados (por defecto 30)
      --pdf / --txt  [--maxpdf 10]                         Descarga PDF y convierte a txt
      --nombre NOMBRE                                      Nombre de la carpeta
  roj "STSJ CL 3456/2024"    Busca un documento por ROJ o ECLI y lo descarga (pdf + txt)
  pdf [ultima|NOMBRE] 1 3 5-7|todos   Descarga y convierte resultados de una busqueda guardada
  txt [ultima|NOMBRE]        Vuelve a convertir a txt los PDF de una busqueda
  ver [ultima|NOMBRE] [N] [-c]   Muestra el listado, el resultado N, o su texto completo (-c)
  lista [-u 20]              Busquedas guardadas
  local TEXTO [-c 2] [--en NOMBRE] [--regex]   Busca dentro de todo lo descargado (sin internet)
  abrir N|URL [--busqueda NOMBRE]   Abre un resultado en Firefox
  lote FICHERO.txt           Hace varias busquedas seguidas (una por linea, con las mismas opciones que buscar)
  estado                     Comprueba que la extension responde
  diag [--mostrar]           Ruta (o contenido) del ultimo diagnostico
""" % VERSION


def main():
    global C, DIAG
    C = cfg()
    if len(sys.argv) < 2 or sys.argv[1] in ('ayuda', '-h', '--help', 'help'):
        out(AYUDA)
        return
    ap = argparse.ArgumentParser(prog='cendoj.py', add_help=False)
    sp = ap.add_subparsers(dest='cmd')

    def comunes(p):
        p.add_argument('-j', '--jurisdiccion')
        p.add_argument('-o', '--organo')
        p.add_argument('-l', '--lugar')
        p.add_argument('-d', '--desde')
        p.add_argument('-a', '--hasta')
        p.add_argument('-n', '--numres')
        p.add_argument('--recurso')
        p.add_argument('-p', '--ponente')
        p.add_argument('-s', '--seccion')
        p.add_argument('--tipo')
        p.add_argument('--discapacidad', action='store_true')

    global PARSER_BUSCAR
    b = sp.add_parser('buscar', add_help=False)
    b.add_argument('texto', nargs='*')
    comunes(b)
    b.add_argument('-r', '--roj')
    b.add_argument('-e', '--ecli')
    b.add_argument('-m', '--max', type=int, default=30)
    b.add_argument('--pdf', action='store_true')
    b.add_argument('--txt', action='store_true')
    b.add_argument('--maxpdf', type=int, default=10)
    b.add_argument('--nombre')
    PARSER_BUSCAR = b
    r = sp.add_parser('roj', add_help=False)
    r.add_argument('valor')
    r.add_argument('--sinpdf', action='store_true')
    comunes(r)
    p = sp.add_parser('pdf', add_help=False)
    p.add_argument('busqueda', nargs='?', default='ultima')
    p.add_argument('numeros', nargs='*')
    p.add_argument('--sintxt', action='store_true')
    t = sp.add_parser('txt', add_help=False)
    t.add_argument('busqueda', nargs='?', default='ultima')
    v = sp.add_parser('ver', add_help=False)
    v.add_argument('busqueda', nargs='?', default='ultima')
    v.add_argument('numero', nargs='?', type=int)
    v.add_argument('-c', '--completo', action='store_true')
    li = sp.add_parser('lista', add_help=False)
    li.add_argument('-u', '--ultimas', type=int, default=20)
    lo = sp.add_parser('local', add_help=False)
    lo.add_argument('texto', nargs='+')
    lo.add_argument('-c', '--contexto', type=int, default=2)
    lo.add_argument('--en')
    lo.add_argument('--regex', action='store_true')
    lo.add_argument('--maximo', type=int, default=20)
    sp.add_parser('estado', add_help=False)
    ab = sp.add_parser('abrir', add_help=False)
    ab.add_argument('que')
    ab.add_argument('--busqueda', default='ultima')
    lt = sp.add_parser('lote', add_help=False)
    lt.add_argument('fichero')
    dg = sp.add_parser('diag', add_help=False)
    dg.add_argument('--mostrar', action='store_true')

    # "pdf 3 5" sin nombre de busqueda -> ultima
    argv = sys.argv[1:]
    if argv and argv[0] == 'pdf' and len(argv) > 1 and (argv[1].isdigit() or argv[1] == 'todos' or re.fullmatch(r'[\d,\-]+', argv[1])):
        argv = ['pdf', 'ultima'] + argv[1:]
    if argv and argv[0] == 'ver' and len(argv) > 1 and argv[1].isdigit():
        argv = ['ver', 'ultima'] + argv[1:]
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        err('orden o opcion no valida. Escribe: python cendoj.py ayuda')
        sys.exit(1)
    if not a.cmd:
        out(AYUDA)
        return
    DIAG = Diag(a.cmd)
    try:
        {'buscar': cmd_buscar, 'roj': cmd_roj, 'pdf': cmd_pdf, 'txt': cmd_txt, 'ver': cmd_ver, 'lista': cmd_lista,
         'local': cmd_local, 'estado': cmd_estado, 'abrir': cmd_abrir, 'diag': cmd_diag,
         'lote': cmd_lote}[a.cmd](a)
    except KeyboardInterrupt:
        err('interrumpido por el usuario')
        sys.exit(130)
    except SystemExit:
        raise
    except BrokenPipeError:
        raise
    except Exception as e:
        DIAG.add('EXCEPCION:\n' + traceback.format_exc())
        err('fallo inesperado: %s. Diagnostico: %s' % (e, DIAG.f))
        sys.exit(1)
    finally:
        if SERVIDOR:
            SERVIDOR.shutdown()


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        try:
            sys.stdout = None
        except Exception:
            pass
