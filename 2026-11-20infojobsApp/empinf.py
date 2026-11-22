#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
empinf.py - Busqueda de empleo en varios sitios desde Termux

SITIOS
  InfoJobs  Abre las paginas en Firefox (tu, orden a orden); la extension
            "EmpInf Guardar" extrae los datos y este programa los guarda
            en .txt en la carpeta actual.
  Isdefe    Lee directamente seleccionisdefe.es (no necesita Firefox).

ORDENES INFOJOBS
  python empinf.py buscar "URL"   Abre esa busqueda o listado de ofertas
                                  (pagina 1 o la que lleve)
  python empinf.py siguiente      Abre la pagina siguiente
  python empinf.py anterior       Abre la pagina anterior
  python empinf.py pagina N       Abre la pagina N
  python empinf.py lista [texto]  Lista las ofertas vistas (filtra por texto)
  python empinf.py abrir 3 7 12   Abre esas ofertas, guarda su ficha y cierra
                                  la pestana (una detras de otra)
  python empinf.py ver N          Muestra la ficha de la oferta N
  python empinf.py guardadas      Lista las fichas ya guardadas
  python empinf.py importar       Procesa los empinf_nav*.json de Descargas
  python empinf.py estado         Busqueda y pagina actuales
  python empinf.py diagnostico    Comprueba Firefox, Descargas y receptor
  python empinf.py nuevas [N]     Solo las ofertas nuevas de verdad (vistas
                                  por 1a vez hoy, o en los ultimos N dias)
  python empinf.py orden [web|real|interes]  Orden de los listados:
                                  InfoJobs, fecha real o interes para ti

ORDENES DE APRENDIZAJE (InfoJobs)
  python empinf.py si 3 7         Esas ofertas te interesan
  python empinf.py no 4 5         Esas no te interesan
  python empinf.py quitar 4       Quita la marca
  python empinf.py porque 7       Por que la oferta 7 tiene ese interes
  python empinf.py reglas         Muestra tus reglas (~/.empinf_reglas.txt)
  python empinf.py aprendizaje    Resumen de lo aprendido

ORDENES INECO (Firefox + extension)
  python empinf.py ineco             Ofertas de Espana > Madrid > oficinas
                                     IT, Sistemas y Consultoria, de la mas
                                     reciente a la mas antigua
  python empinf.py ineco it          Solo una oficina (it, sistemas,
                                     consultoria, civil, otras); se pueden
                                     poner varias
  python empinf.py ineco ver 3 [5]   Abre la oferta, la guarda en .txt y la
                                     muestra
  (anade "pdf" a cualquiera para crear tambien un .pdf)

ORDENES LINKEDIN EMPLEOS (Firefox + extension, con tu sesion abierta)
  python empinf.py linkedin          Pagina principal de Empleos: "Empleos
                                     que coinciden con tu perfil" y "Mas
                                     empleos para ti" de N en N (pregunta
                                     si quieres mas)
  python empinf.py linkedin lote 20  Cuantos "mas empleos" cada vez (def. 10)
  python empinf.py linkedin coinciden  Todos los que coinciden con tu perfil
  python empinf.py linkedin reciente 1  Abre una de tus busquedas recientes
  python empinf.py linkedin buscar "URL"   Una busqueda de LinkedIn
  python empinf.py linkedin siguiente | pagina N   Otras paginas
  python empinf.py linkedin registro [guardados|en curso|solicitados|
                                     entrevistas|archivados]
  python empinf.py linkedin perspectivas   "Mis perspectivas profesionales"
  python empinf.py linkedin ver 3 [5]      Oferta completa en .txt
  (anade "pdf" a cualquiera para crear tambien un .pdf)

ORDENES ISDEFE
  python empinf.py isdefe [N]        Ultimos N puestos publicados (def. 15),
                                     del mas reciente al mas antiguo
  python empinf.py isdefe ver 3 [5]  Descarga y muestra el perfil del puesto

ARCHIVOS (en la carpeta desde la que ejecutas las ordenes)
  busqueda_NOMBRE_pNN.txt   InfoJobs: listado de cada pagina
  oferta_NNN_....txt        InfoJobs: ficha completa de cada oferta
  isdefe_listado.txt        Isdefe: ultimo listado
  isdefe_NN_REF_....txt     Isdefe: perfil de cada puesto
  .empinf_estado.json, .empinf_isdefe.json, .empinf_datos/  (internos)

VARIABLES OPCIONALES
  EMPINF_DESCARGAS  carpeta de descargas (por defecto ~/storage/downloads)
  FF_PKG            paquete de Firefox (por defecto org.mozilla.firefox;
                    Nightly: org.mozilla.fenix)
  EMPINF_ESPERA     segundos entre oferta y oferta al abrir varias (min. 10)
  EMPINF_VOLVER     0 = no volver a Termux al terminar (por defecto si)
"""

import os
import re
import sys
import json
import glob
import hashlib
import math
import time
import shutil
import random
import string
import textwrap
import subprocess
import threading
import unicodedata
import urllib.request
import urllib.error
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, quote

ANCHO = 78
ETIQ = 18
DESCARGAS = os.path.expanduser(os.environ.get('EMPINF_DESCARGAS',
                                              '~/storage/downloads'))
FF_PKG = os.environ.get('FF_PKG', 'org.mozilla.firefox')
ESPERA = max(10, int(os.environ.get('EMPINF_ESPERA', '12') or 12))
VOLVER = os.environ.get('EMPINF_VOLVER', '1') != '0'   # volver a Termux
LIMITE = 75                     # segundos maximos esperando un guardado
PUERTO = 8766                   # la extension envia aqui los datos
ESTADO = '.empinf_estado.json'
DATOS = '.empinf_datos'


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def slug(texto, n=40):
    t = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore')
    t = re.sub(r'[^a-z0-9]+', '-', t.decode().lower()).strip('-')
    return (t or 'x')[:n].strip('-')


def campo(etiqueta, valor):
    if not valor:
        return []
    if len(etiqueta) + 2 > ETIQ:             # etiqueta larga: valor debajo
        return [etiqueta + ':'] + textwrap.wrap(
            str(valor), ANCHO, initial_indent=' ' * ETIQ,
            subsequent_indent=' ' * ETIQ, break_long_words=False,
            break_on_hyphens=False)
    return textwrap.TextWrapper(
        width=ANCHO, initial_indent=(etiqueta + ':').ljust(ETIQ),
        subsequent_indent=' ' * ETIQ, break_long_words=False,
        break_on_hyphens=False).wrap(str(valor))


def envolver(texto):
    out = []
    for linea in (texto or '').split('\n'):
        if not linea.strip():
            out.append('')
        elif linea.lstrip().startswith(('-', '•', '*')):
            out += textwrap.wrap(linea.strip(), ANCHO, subsequent_indent='  ')
        else:
            out += textwrap.wrap(linea.strip(), ANCHO)
    return out


def parrafos_resumen(texto):
    out = []
    for i, trozo in enumerate(re.split(r'\s+-\s+', texto or '')):
        if not trozo:
            continue
        out += textwrap.wrap(trozo if i == 0 else '- ' + trozo, ANCHO,
                             subsequent_indent='' if i == 0 else '  ')
    return out


def recorta(texto, n):
    texto = texto or '-'
    return texto if len(texto) <= n else texto[:n - 1] + '~'


def cargar_estado():
    if os.path.exists(ESTADO):
        with open(ESTADO, encoding='utf-8') as f:
            return json.load(f)
    return {'url': '', 'pagina': 0, 'clave': '', 'total': None,
            'siguiente': None, 'ofertas': {}, 'orden': []}


def guardar_estado(est):
    with open(ESTADO + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(est, f, ensure_ascii=False, indent=1)
    os.replace(ESTADO + '.tmp', ESTADO)


def num_de(est, oid):
    for n, o in est['ofertas'].items():
        if o['id'] == oid:
            return int(n)
    return None


def url_pagina(url, pagina):
    p = urlparse(url.split('#')[0])
    q = parse_qs(p.query, keep_blank_values=True)
    q['page'] = [str(pagina)]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


# ----------------------------------------------------------------------
# Firefox y descargas
# ----------------------------------------------------------------------
def abrir_en_firefox(url, cerrar=False, extra=''):
    iniciar_receptor()
    token = ''.join(random.choices(string.ascii_lowercase + string.digits,
                                   k=10))
    marca = ('#empinfc-' if cerrar else '#empinf-') + token + extra
    try:
        r = subprocess.run(['am', 'start', '--user', '0', '-a',
                            'android.intent.action.VIEW', '-d',
                            url.split('#')[0] + marca, FF_PKG],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True)
    except FileNotFoundError:
        sys.exit('No encuentro la orden "am". Este programa debe ejecutarse '
                 'en Termux (Android).')
    if r.returncode != 0 or 'Error' in (r.stderr or ''):
        sys.exit(f'No se pudo abrir Firefox ({FF_PKG}): {r.stderr.strip()}')
    return token


# ----------------------------------------------------------------------
# Receptor: la extension envia los datos por http://127.0.0.1:8766/empinf
# ----------------------------------------------------------------------
RECIBIDOS = {}
_receptor_activo = False


class _Receptor(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            datos = json.loads(self.rfile.read(n).decode('utf-8'))
            RECIBIDOS[datos.get('token', '')] = datos
            self.send_response(200)
        except (ValueError, OSError):
            self.send_response(400)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(b'ok')

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def log_message(self, *args):
        pass                                    # sin mensajes en pantalla


def iniciar_receptor():
    global _receptor_activo
    if _receptor_activo:
        return
    try:
        srv = HTTPServer(('127.0.0.1', PUERTO), _Receptor)
    except OSError:
        print(f'(Aviso: el puerto {PUERTO} esta ocupado; se usara solo '
              'Descargas. Cierra otros empinf.py abiertos.)')
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _receptor_activo = True


def guardar_copia(datos, token):
    os.makedirs(DATOS, exist_ok=True)
    with open(os.path.join(DATOS, f'empinf_{token}.json'), 'w',
              encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)


def volver_a_termux():
    """Trae Termux al frente cuando ya han llegado los datos."""
    if not VOLVER:
        return
    try:
        subprocess.run(['am', 'start', '--user', '0', '-n',
                        'com.termux/com.termux.app.TermuxActivity'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


ESPERA_CAPTCHA = 180            # segundos para resolver un captcha a mano


def esperar_datos(token, limite=LIMITE, volver=True):
    hay_descargas = os.path.isdir(DESCARGAS)
    fin = time.time() + limite
    sys.stdout.write('Esperando a Firefox')
    sys.stdout.flush()
    avisado = False
    while time.time() < fin:
        if token in RECIBIDOS:                          # via Termux
            datos = RECIBIDOS.pop(token)
            if datos.get('tipo') == 'captcha':
                # No es el final: se espera a que lo resuelvas en Firefox
                if not avisado:
                    print('\n  InfoJobs pide verificar que eres humano. '
                          'Resuelvelo en Firefox;\n  en cuanto cargue la '
                          'pagina sigo yo (espero hasta 3 minutos)', end='')
                    avisado = True
                fin = max(fin, time.time() + ESPERA_CAPTCHA)
                continue
            guardar_copia(datos, token)
            print(' listo.')
            if volver and datos.get('tipo') != 'captcha':
                volver_a_termux()
            return datos
        if hay_descargas:                               # via Descargas
            for ruta in glob.glob(os.path.join(DESCARGAS, f'empinf_{token}*')):
                if '_captcha' in ruta:
                    try:
                        os.remove(ruta)
                    except OSError:
                        pass
                    if not avisado:
                        print('\n  Captcha en Firefox: resuelvelo, sigo '
                              'esperando (hasta 3 minutos)', end='')
                        avisado = True
                    fin = max(fin, time.time() + ESPERA_CAPTCHA)
                    continue
                time.sleep(1)
                try:
                    with open(ruta, encoding='utf-8') as f:
                        datos = json.load(f)
                except (ValueError, OSError):
                    continue
                os.makedirs(DATOS, exist_ok=True)
                shutil.move(ruta, os.path.join(DATOS, os.path.basename(ruta)))
                print(' listo (desde Descargas).')
                if volver and datos.get('tipo') != 'captcha':
                    volver_a_termux()
                return datos
        sys.stdout.write('.')
        sys.stdout.flush()
        time.sleep(1)
    print()
    print('No ha llegado nada. Mira el aviso que muestra la extension abajo en\n'
          'la pagina de Firefox:\n'
          '  - No sale ningun aviso: la extension no esta instalada o activada.\n'
          '  - "activa (sin orden de Termux)": Firefox perdio la marca de la\n'
          '    URL; avisa a quien te hizo el programa.\n'
          '  - "esperando a que cargue..." todo el rato: InfoJobs ha cambiado\n'
          '    su web o la pagina no termino de cargar.\n'
          '  - "fallo al guardar (...)": copia el mensaje completo.\n'
          'Tambien puedes ejecutar:  python empinf.py diagnostico')
    return None


def cmd_diagnostico():
    print(f'Carpeta actual:   {os.getcwd()}')
    print(f'Descargas:        {DESCARGAS} '
          f'({"existe" if os.path.isdir(DESCARGAS) else "NO EXISTE: ejecuta termux-setup-storage"})')
    try:
        r = subprocess.run(['pm', 'list', 'packages', 'mozilla'],
                           capture_output=True, text=True, timeout=20)
        paquetes = [x.replace('package:', '') for x in r.stdout.split()]
        print(f'Firefox instalados: {", ".join(paquetes) or "ninguno"}')
        print(f'FF_PKG en uso:    {FF_PKG}'
              + ('' if FF_PKG in paquetes else '   <-- NO INSTALADO'))
    except (OSError, subprocess.SubprocessError):
        print('No se pudo consultar los paquetes (pm).')
    iniciar_receptor()
    print(f'Receptor Termux:  {"escuchando en 127.0.0.1:" + str(PUERTO) if _receptor_activo else "NO disponible"}')
    if os.path.isdir(DESCARGAS):
        ij = sorted(glob.glob(os.path.join(DESCARGAS, 'empinf_*')))
        print(f'Archivos empinf_ en Descargas: {len(ij)}')
        for x in ij[-5:]:
            print('   ' + os.path.basename(x))
        try:
            recientes = sorted((os.path.join(DESCARGAS, f)
                                for f in os.listdir(DESCARGAS)),
                               key=os.path.getmtime)[-5:]
            print('Ultimos archivos en Descargas:')
            for x in recientes:
                print('   ' + time.strftime('%d/%m %H:%M', time.localtime(
                    os.path.getmtime(x))) + '  ' + os.path.basename(x))
        except OSError as e:
            print(f'No se puede leer Descargas: {e}')


def aviso_captcha(datos):
    if datos and datos.get('tipo') == 'captcha':
        print('\nInfoJobs pide comprobar que eres humano (captcha).\n'
              'Resuelvelo en Firefox y repite la orden dentro de un rato.\n'
              'Si pasa a menudo, espacia mas las ordenes.')
        return True
    return False


# ----------------------------------------------------------------------
# Busqueda
# ----------------------------------------------------------------------
def id_busqueda(url):
    """Identifica una busqueda sin tener en cuenta la pagina ni los
    parametros que no cambian los resultados."""
    p = urlparse((url or '').split('#')[0])
    q = parse_qs(p.query, keep_blank_values=False)
    for k in ('page', 'referer', 'segmentId', 'searchId', 'applicationOrigin'):
        q.pop(k, None)
    return p.path.rstrip('/') + '?' + urlencode(sorted(q.items()), doseq=True)


def empezar_de_nuevo(est):
    """Otra busqueda en la misma carpeta: se guarda la anterior aparte y
    la numeracion vuelve a empezar en 1."""
    copia = f'.empinf_estado_{slug(est.get("clave"), 30)}_' \
            f'{time.strftime("%Y%m%d_%H%M%S")}.json'
    with open(copia, 'w', encoding='utf-8') as f:
        json.dump(est, f, ensure_ascii=False, indent=1)
    print(f'Nueva busqueda: la numeracion empieza en 1. La anterior '
          f'("{est.get("clave")}") queda guardada en {copia}.\n'
          'Consejo: usa una carpeta distinta para cada busqueda.')
    est.clear()
    est.update({'url': '', 'pagina': 0, 'clave': '', 'total': None,
                'siguiente': None, 'ofertas': {}, 'orden': []})


# ----------------------------------------------------------------------
# Historial: fecha REAL en que viste cada oferta por primera vez
# (InfoJobs republica las ofertas para que parezcan nuevas)
# ----------------------------------------------------------------------
HISTORIAL = os.path.expanduser(os.environ.get('EMPINF_HISTORIAL',
                                              '~/.empinf_historial.json'))
CONFIG = os.path.expanduser('~/.empinf_config.json')
PATRON_FECHA = re.compile(r'^(hace\s+\d+\s*\w*|ayer|hoy|nueva|\d{1,2}\s+'
                          r'(ene|feb|mar|abr|may|jun|jul|ago|sept?|oct|nov|'
                          r'dic)\w*\.?)$', re.I)


def _norm(t):
    t = unicodedata.normalize('NFKD', t or '').encode('ascii', 'ignore')
    return re.sub(r'[^a-z0-9]+', ' ', t.decode().lower()).strip()


def huella(o):
    """Identifica la oferta por su contenido visible (puesto, empresa y
    lugar), sin la fecha: si InfoJobs la vuelve a publicar, o la crea de
    nuevo con otro numero, sigue siendo la misma oferta."""
    base = '|'.join(_norm(o.get(k)) for k in ('titulo', 'empresa',
                                              'ubicacion'))
    return hashlib.sha1(base.encode('utf-8')).hexdigest()[:12]


def cargar_json(ruta, vacio):
    try:
        with open(ruta, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return vacio


def guardar_json(ruta, datos):
    with open(ruta + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=1)
    os.replace(ruta + '.tmp', ruta)


def corregir_modalidad(o):
    """A veces la fecha de InfoJobs ("Hace 2d") llega en el hueco de la
    modalidad: se pasa a su sitio."""
    mod = (o.get('modalidad') or '').strip()
    if mod and PATRON_FECHA.match(mod):
        if not o.get('publicada'):
            o['publicada'] = mod
        o['modalidad'] = ''


def anotar_historial(ofertas):
    """Apunta la primera vez que se ve cada oferta. Devuelve cuantas son
    nuevas de verdad (nunca vistas antes)."""
    h = cargar_json(HISTORIAL, {'por_id': {}, 'ofertas': {}})
    hoy = time.strftime('%Y-%m-%d')
    nuevas = 0
    for o in ofertas:
        clave = h['por_id'].get(o.get('id')) or huella(o)
        reg = h['ofertas'].get(clave)
        if reg is None:
            reg = {'primera': hoy, 'titulo': o.get('titulo', ''),
                   'empresa': o.get('empresa', ''), 'veces': 0, 'ids': []}
            h['ofertas'][clave] = reg
            nuevas += 1
        if reg.get('ultima') != hoy:
            reg['veces'] = reg.get('veces', 0) + 1
        reg['ultima'] = hoy
        reg['fecha_web'] = o.get('publicada', '')
        if o.get('id') and o['id'] not in reg['ids']:
            reg['ids'].append(o['id'])
            h['por_id'][o['id']] = clave
        o['huella'] = clave
        o['primera_vista'] = reg['primera']
    guardar_json(HISTORIAL, h)
    return nuevas


# ======================================================================
# APRENDIZAJE (opcion B): reglas tuyas + clasificador que aprende de tus
# marcas "si" / "no". Solo usa los datos del listado.
# ======================================================================
APRENDIZAJE = os.path.expanduser(os.environ.get(
    'EMPINF_APRENDIZAJE', '~/.empinf_aprendizaje.json'))
REGLAS = os.path.expanduser(os.environ.get('EMPINF_REGLAS',
                                           '~/.empinf_reglas.txt'))
PLANTILLA_REGLAS = """# REGLAS DE EMPINF  (edita con:  nano ~/.empinf_reglas.txt)
#
# Una regla por linea:  TIPO: valor1, valor2, ...
# Las lineas que empiezan por # no cuentan. Mayusculas y acentos da igual.
# "subir" y "bajar" buscan en el puesto y en el resumen del listado;
# "descartar" solo en el puesto.
#
#   descartar:            si el PUESTO contiene la palabra, la oferta va
#                         al final con interes 0 (X)
#   subir:                +15 de interes por cada palabra encontrada
#   bajar:                -15 de interes por cada palabra encontrada
#   empresa_descartar:    empresas que no quieres ver
#   empresa_subir:        empresas que te interesan (+15)
#   modalidad_descartar:  pres, hib o tele
#   salario_minimo:       en miles de euros/ano (ej. 25). Si la oferta
#                         indica un salario maximo por debajo: -30.
#                         Si no indica salario, no cuenta.
#
# Ejemplos (quita el # del principio para activarlos):
#
# descartar: practicas, becario, operario, docente, profesor
# subir: vb.net, vba, visual basic, c#, .net, access, sql server
# bajar: java, cobol, php, react
# empresa_descartar:
# modalidad_descartar: pres
# salario_minimo: 25
"""


def _limpio(t):
    t = unicodedata.normalize('NFKD', t or '').encode('ascii', 'ignore')
    return ' ' + re.sub(r'\s+', ' ', t.decode().lower()) + ' '


def leer_reglas():
    if not os.path.exists(REGLAS):
        with open(REGLAS, 'w', encoding='utf-8') as f:
            f.write(PLANTILLA_REGLAS)
    reglas = {}
    with open(REGLAS, encoding='utf-8') as f:
        for linea in f:
            linea = linea.strip()
            if not linea or linea.startswith('#') or ':' not in linea:
                continue
            tipo, valores = linea.split(':', 1)
            tipo = _limpio(tipo).strip().replace(' ', '_')
            vals = [_limpio(v).strip() for v in valores.split(',')
                    if v.strip()]
            reglas.setdefault(tipo, []).extend(vals)
    return reglas


def _contiene(texto, palabra):
    """Palabra completa; admite simbolos como c#, .net, vb.net."""
    return re.search(r'(?<![a-z0-9])' + re.escape(palabra) +
                     r'(?![a-z0-9])', texto) is not None


def _salario_max(o):
    nums = [int(x.replace('.', '')) for x in
            re.findall(r'\d[\d.]*', (o.get('salario') or '').replace(
                '\xa0', ' '))]
    if not nums or 'hora' in (o.get('salario') or '').lower():
        return None
    m = max(nums)
    if 'mes' in (o.get('salario') or '').lower():
        m *= 14
    return m / 1000


def aplicar_reglas(o, reglas):
    """Devuelve (descartada, ajuste, motivos).
    'descartar' mira solo el PUESTO (asi "buenas practicas" del resumen no
    descarta nada); 'subir' y 'bajar' miran puesto y resumen."""
    titulo = _limpio(o.get('titulo'))
    texto = _limpio((o.get('titulo') or '') + ' ' + (o.get('resumen') or ''))
    empresa = _limpio(o.get('empresa'))
    motivos, ajuste, fuera = [], 0, False
    for p in reglas.get('descartar', []):
        if _contiene(titulo, p):
            fuera = True
            motivos.append(f'descartada por "{p}"')
    for p in reglas.get('empresa_descartar', []):
        if p and p in empresa:
            fuera = True
            motivos.append(f'empresa descartada "{p}"')
    mod = modalidad_corta(o.get('modalidad'))
    if mod and mod in reglas.get('modalidad_descartar', []):
        fuera = True
        motivos.append(f'modalidad {mod} descartada')
    for p in reglas.get('subir', []):
        if _contiene(texto, p):
            ajuste += 15
            motivos.append(f'+15 "{p}"')
    for p in reglas.get('bajar', []):
        if _contiene(texto, p):
            ajuste -= 15
            motivos.append(f'-15 "{p}"')
    for p in reglas.get('empresa_subir', []):
        if p and p in empresa:
            ajuste += 15
            motivos.append(f'+15 empresa "{p}"')
    for v in reglas.get('salario_minimo', [])[:1]:
        try:
            minimo = float(v.replace(',', '.'))
        except ValueError:
            continue
        smax = _salario_max(o)
        if smax is not None and smax < minimo:
            ajuste -= 30
            motivos.append(f'-30 salario hasta {smax:g}k < {minimo:g}k')
    return fuera, ajuste, motivos


VACIAS = set("""de la el en y a los las del un una por con para se que es al
lo como mas o su sus nos te tu ser son esta este estas estos sera muy
tambien entre sin sobre desde hasta donde cuando todo toda todos todas
buscamos ofrecemos empresa trabajo equipo experiencia persona personas
perfil puesto candidato candidata funciones requisitos valorara""".split())


def rasgos(o):
    """Lo que el clasificador 've' de una oferta del listado."""
    r = set()
    titulo = _limpio(o.get('titulo'))
    palabras = re.findall(r'[a-z0-9#+.]+', titulo)
    palabras = [w.strip('.') for w in palabras
                if len(w.strip('.')) > 1 and w.strip('.') not in VACIAS]
    r.update('t:' + w for w in palabras)
    r.update('t:' + a + '_' + b for a, b in zip(palabras, palabras[1:]))
    for w in re.findall(r'[a-z0-9#+.]+', _limpio(o.get('resumen'))):
        w = w.strip('.')
        if len(w) > 2 and w not in VACIAS:
            r.add('r:' + w)
    if o.get('empresa'):
        r.add('e:' + _limpio(o['empresa']).strip())
    if o.get('ubicacion'):
        r.add('l:' + _limpio(o['ubicacion']).strip())
    r.add('m:' + (modalidad_corta(o.get('modalidad')) or 'nd'))
    if o.get('contrato'):
        r.add('c:' + _limpio(o['contrato']).strip())
    if o.get('jornada'):
        r.add('j:' + _limpio(o['jornada']).strip())
    smax = _salario_max(o)
    r.add('s:' + ('nd' if smax is None else
                  '<25' if smax < 25 else '25-35' if smax < 35 else
                  '35-45' if smax < 45 else '45+'))
    return r


def cargar_aprendizaje():
    return cargar_json(APRENDIZAJE, {'marcas': {}})


class Clasificador:
    """Naive Bayes (Bernoulli) sobre los rasgos del listado."""

    def __init__(self, marcas):
        self.n = {1: 0, 0: 0}
        self.cuenta = {1: {}, 0: {}}
        for m in marcas.values():
            c = 1 if m['voto'] > 0 else 0
            self.n[c] += 1
            for f in rasgos(m['datos']):
                self.cuenta[c][f] = self.cuenta[c].get(f, 0) + 1

    def listo(self):
        return self.n[1] >= 1 and self.n[0] >= 1

    def peso(self, f):
        p1 = (self.cuenta[1].get(f, 0) + 0.5) / (self.n[1] + 1)
        p0 = (self.cuenta[0].get(f, 0) + 0.5) / (self.n[0] + 1)
        return math.log(p1 / p0)

    # cuanto cuenta cada tipo de dato (el resumen es largo y ruidoso)
    FACTOR = {'t': 1.0, 'e': 1.0, 'm': 1.0, 's': 0.8, 'l': 0.6, 'c': 0.5,
              'j': 0.5, 'r': 0.12}

    def conocido(self, f):
        return self.cuenta[1].get(f, 0) + self.cuenta[0].get(f, 0) > 0

    def puntua(self, o):
        """0-100 (50 = sin opinion)."""
        if not self.listo():
            return 50, []
        pesos = [(self.peso(f) * self.FACTOR.get(f[0], 1.0), f)
                 for f in rasgos(o) if self.conocido(f)]
        prior = math.log((self.n[1] + 1) / (self.n[0] + 1)) * 0.5
        total = prior + sum(w for w, _ in pesos)
        # con pocas marcas se atenua la seguridad
        confianza = min(1.0, (self.n[1] + self.n[0]) / 60)
        total *= 0.25 + 0.75 * confianza
        prob = 1 / (1 + math.exp(-max(-30, min(30, total))))
        return round(prob * 100), sorted(pesos, reverse=True)


def calcular_interes(est):
    """Anade a cada oferta 'interes' (0-100), 'descartada' y 'voto'."""
    marcas = cargar_aprendizaje()['marcas']
    clf = Clasificador(marcas)
    reglas = leer_reglas()
    for o in est['ofertas'].values():
        base, _ = clf.puntua(o)
        fuera, ajuste, _ = aplicar_reglas(o, reglas)
        o['descartada'] = fuera
        o['interes'] = 0 if fuera else max(0, min(100, base + ajuste))
        voto = marcas.get(o.get('huella') or huella(o), {}).get('voto')
        o['voto'] = voto
    return clf


def cmd_marcar(numeros, voto):
    est = cargar_estado()
    apr = cargar_aprendizaje()
    hechas = []
    for n in numeros:
        o = est['ofertas'].get(str(n))
        if not o:
            print(f'No hay oferta numero {n}.')
            continue
        clave = o.get('huella') or huella(o)
        if voto is None:
            apr['marcas'].pop(clave, None)
        else:
            apr['marcas'][clave] = {
                'voto': voto, 'fecha': time.strftime('%Y-%m-%d'),
                'datos': {k: o.get(k, '') for k in (
                    'titulo', 'empresa', 'ubicacion', 'modalidad',
                    'contrato', 'jornada', 'salario', 'resumen')}}
        hechas.append(n)
    guardar_json(APRENDIZAJE, apr)
    si = sum(1 for m in apr['marcas'].values() if m['voto'] > 0)
    no = len(apr['marcas']) - si
    que = {1: 'me interesan', 0: 'no me interesan', None: 'sin marca'}[voto]
    if hechas:
        print(f'Marcadas como "{que}": ' + ' '.join(map(str, hechas)))
    print(f'Aprendizaje: {si} "si" y {no} "no" en total.'
          + ('' if si and no else
             '  (hace falta al menos un "si" y un "no" para aprender)'))


def cmd_porque(n):
    est = cargar_estado()
    o = est['ofertas'].get(str(n))
    if not o:
        sys.exit(f'No hay oferta numero {n}.')
    clf = calcular_interes(est)
    base, pesos = clf.puntua(o)
    fuera, ajuste, motivos = aplicar_reglas(o, leer_reglas())
    print(f'\n[{n}] {o["titulo"]}  ({o["empresa"]})')
    print(f'Interes: {o["interes"]}' + ('  (DESCARTADA)' if fuera else ''))
    print(f'  Aprendido de tus marcas: {base}'
          + ('' if clf.listo() else '  (aun sin datos: faltan marcas)'))
    print(f'  Ajuste por reglas:       {ajuste:+d}')
    if motivos:
        print('  Reglas aplicadas: ' + '; '.join(motivos))
    if clf.listo():
        def nombre(f):
            t, v = f.split(':', 1)
            return {'t': 'puesto', 'r': 'resumen', 'e': 'empresa',
                    'l': 'lugar', 'm': 'modalidad', 'c': 'contrato',
                    'j': 'jornada', 's': 'salario k€'}.get(t, t) + \
                ' "' + v.replace('_', ' ') + '"'
        sube = [(w, f) for w, f in pesos if w > 0.15][:6]
        baja = [(w, f) for w, f in pesos if w < -0.15][-6:]
        if sube:
            print('  Sube por:  ' + ', '.join(nombre(f) for _, f in sube))
        if baja:
            print('  Baja por:  ' + ', '.join(nombre(f) for _, f in baja))
    if o.get('voto') is not None:
        print(f'  Tu marca: {"si" if o["voto"] else "no"}')


def cmd_reglas():
    leer_reglas()                       # crea la plantilla si no existe
    print(f'Archivo de reglas: {REGLAS}')
    print('Editar:  nano ~/.empinf_reglas.txt\n')
    with open(REGLAS, encoding='utf-8') as f:
        activas = [l.rstrip() for l in f
                   if l.strip() and not l.lstrip().startswith('#')]
    if activas:
        print('Reglas activas:')
        for l in activas:
            print('  ' + l)
    else:
        print('No hay reglas activas (todas empiezan por #).')


def cmd_aprendizaje():
    apr = cargar_aprendizaje()['marcas']
    si = [m for m in apr.values() if m['voto'] > 0]
    no = [m for m in apr.values() if m['voto'] == 0]
    print(f'Marcas guardadas: {len(si)} "si", {len(no)} "no"   ({APRENDIZAJE})')
    clf = Clasificador(apr)
    if not clf.listo():
        print('Aun no aprende: marca al menos una oferta con "si" y otra '
              'con "no".')
        return
    todos = set(clf.cuenta[1]) | set(clf.cuenta[0])
    pesos = sorted(((clf.peso(f) * clf.FACTOR.get(f[0], 1.0), f)
                    for f in todos
                    if clf.cuenta[1].get(f, 0) + clf.cuenta[0].get(f, 0)
                    >= 2), reverse=True)
    print('\nLo que mas SUBE el interes:')
    for w, f in pesos[:10]:
        print(f'  {w:+.1f}  {f}')
    print('\nLo que mas BAJA el interes:')
    for w, f in pesos[-10:][::-1]:
        print(f'  {w:+.1f}  {f}')


def orden_elegido():
    return cargar_json(CONFIG, {}).get('orden', 'web')


def ordenar(numeros, est):
    """'web' = orden de InfoJobs; 'real' = primero las vistas por primera
    vez mas recientemente."""
    orden = orden_elegido()
    if orden == 'interes':
        calcular_interes(est)
        return sorted(numeros, key=lambda n: (
            not est['ofertas'][str(n)].get('descartada'),
            est['ofertas'][str(n)].get('interes', 50), -int(n)),
            reverse=True)
    if orden != 'real':
        return list(numeros)
    return sorted(numeros, key=lambda n: (
        est['ofertas'][str(n)].get('primera_vista', ''), -int(n)),
        reverse=True)


def visto_corto(fecha):
    if not fecha:
        return '-'
    if fecha == time.strftime('%Y-%m-%d'):
        return 'HOY'
    return f'{fecha[8:10]}/{fecha[5:7]}'


def cmd_orden(args):
    cfg = cargar_json(CONFIG, {})
    if args and args[0].lower() in ('web', 'real', 'interes', 'interés'):
        args[0] = args[0].lower().replace('é', 'e')
        cfg['orden'] = args[0].lower()
        guardar_json(CONFIG, cfg)
    actual = cfg.get('orden', 'web')
    print(f'Orden de los listados: {actual}')
    print('  web   = como las publica InfoJobs'
          + ('   <- actual' if actual == 'web' else ''))
    print('  real  = primero las que has visto por primera vez mas '
          'recientemente' + ('   <- actual' if actual == 'real' else ''))
    print('  interes = primero las que mas te pueden interesar (reglas + '
          'marcas)' + ('   <- actual' if actual == 'interes' else ''))
    print('Cambiar:  python empinf.py orden web | real | interes')


def registrar_busqueda(est, datos):
    """Anade las ofertas de una pagina al estado y escribe su .txt."""
    if est.get('url') and id_busqueda(est['url']) != id_busqueda(datos['url']):
        empezar_de_nuevo(est)
    est['url'] = datos['url']
    est['pagina'] = datos.get('pagina') or 1
    est['clave'] = datos.get('clave', '')
    est['total'] = datos.get('total')
    est['siguiente'] = datos.get('hay_siguiente')
    numeros = []
    for o in datos.get('ofertas', []):
        corregir_modalidad(o)
    est['nuevas_hoy'] = anotar_historial(datos.get('ofertas', []))
    for o in datos.get('ofertas', []):
        n = num_de(est, o['id'])
        if n is not None:                       # ya estaba: actualizar
            viejo = est['ofertas'][str(n)]
            for k in ('primera_vista', 'huella', 'publicada', 'modalidad'):
                viejo[k] = o.get(k, viejo.get(k))
        if n is None:
            n = len(est['ofertas']) + 1
            o['pagina'] = est['pagina']
            o['fichero'] = ''
            est['ofertas'][str(n)] = o
        numeros.append(n)
    guardar_estado(est)
    escribir_txt_busqueda(est, datos, numeros)
    return numeros


def modalidad_corta(m):
    m = (m or '').lower()
    if 'tele' in m or 'remot' in m:
        return 'tele'
    if 'brid' in m:
        return 'hib'
    if 'presen' in m:
        return 'pres'
    return ''


def salario_corto(s):
    """Salario compacto para la lista:
         '40.000 € - 50.000 € Bruto/año'  ->  '40-50'      (miles/año)
         'Más de 25.000 €'                ->  '>25'
         '1.200 € - 1.500 € Bruto/mes'    ->  '1,2-1,5/mes' (miles/mes)
         '14 € - 20 € Bruto/hora'         ->  '14-20/h'     (euros/hora)
    """
    s = (s or '').replace('\xa0', ' ')
    if not s.strip():
        return ''
    numeros = [int(x.replace('.', '')) for x in re.findall(r'\d[\d.]*', s)]
    if not numeros:
        return ''                   # "Salario no disponible", etc.
    sl = s.lower()
    if 'hora' in sl:
        fmt, sufijo = (lambda n: str(n)), '/h'
    else:
        def fmt(n):
            if n < 1000:
                return str(n)
            k = n / 1000
            return (f'{k:.0f}' if k == int(k) else f'{k:.1f}').replace('.', ',')
        sufijo = '/mes' if 'mes' in sl else ''
    cifras = '-'.join(fmt(n) for n in dict.fromkeys(numeros))
    if 'más de' in sl or 'mas de' in sl:
        cifras = '>' + cifras
    elif 'hasta' in sl or 'menos de' in sl:
        cifras = '<' + cifras
    return cifras + sufijo


FIJO = 5 + 2 + 2 + 4 + 2 + 5 + 2 + 11 + 2 + 4  # ..., salario, interes


def columnas():
    """Anchos de Puesto y Empresa segun el ancho de la pantalla.
    Con pantalla suficiente el puesto tiene al menos 40 caracteres y se
    lleva la mayor parte del espacio que sobre."""
    total = shutil.get_terminal_size((ANCHO, 24)).columns
    fijo = FIJO
    libre = total - 1 - fijo
    if libre < 40 + 14:                    # pantalla estrecha
        return max(20, libre - 14), 14
    empresa = min(max(14, libre - 40 - 30), 26)
    return libre - empresa, empresa


def fila(n, o, anchos=None):
    puesto, empresa = anchos or columnas()
    mod = modalidad_corta(o.get('modalidad')) or '-'
    marca = '*' if o.get('fichero') else ' '
    return (f'{n:>3}{marca} {recorta(o["titulo"], puesto):<{puesto}}  '
            f'{recorta(o["empresa"], empresa):<{empresa}}  '
            f'{mod:<4}  {visto_corto(o.get("primera_vista")):<5}  '
            f'{recorta(salario_corto(o.get("salario")), 11):<11}  '
            f'{interes_corto(o)}').rstrip()


def interes_corto(o):
    if o.get('descartada'):
        return '  X'
    marca = {1: '+', 0: '-'}.get(o.get('voto'), ' ')
    return f'{o.get("interes", 50):>3}{marca}'


def cabecera(anchos=None):
    puesto, empresa = anchos or columnas()
    linea = FIJO - 2 - 2 + puesto + empresa
    return (f'{"N":>3}  {"Puesto":<{puesto}}  {"Empresa":<{empresa}}  '
            f'{"Mod.":<4}  {"1ªvez":<5}  {"Sal. (k€)":<11}  Int.\n'
            + '-' * linea)


def mostrar_pagina(est, numeros):
    total = f' de {est["total"]} ofertas' if est.get('total') else ''
    orden = orden_elegido()
    nuevas = est.get('nuevas_hoy')
    print(f'\n"{est["clave"]}"  -  pagina {est["pagina"]}{total}   '
          f'(orden: {orden}'
          + (f', nuevas de verdad: {nuevas}' if nuevas is not None else '')
          + ')\n')
    calcular_interes(est)
    anchos = columnas()
    print(cabecera(anchos))
    for n in ordenar(numeros, est):
        print(fila(n, est['ofertas'][str(n)], anchos))
    print('\n(* = ficha ya guardada)   '
          'Siguiente: python empinf.py siguiente   '
          'Abrir: python empinf.py abrir N ...'
          if est.get('siguiente') is not False else
          '\nEs la ultima pagina de la busqueda.')


def escribir_txt_busqueda(est, datos, numeros):
    lin = [f'BUSQUEDA "{est["clave"]}" - PAGINA {est["pagina"]}',
           f'Guardada: {datos.get("guardado", "")[:16].replace("T", " ")}'
           + (f'   Total de la busqueda: {est["total"]} ofertas'
              if est.get('total') else ''),
           f'URL: {datos["url"]}', '']
    for n in numeros:
        o = est['ofertas'][str(n)]
        lin += ['=' * ANCHO, f'[{n}] {o["titulo"]}', '=' * ANCHO]
        lin += campo('Empresa', o.get('empresa'))
        lin += campo('Ubicacion', o.get('ubicacion'))
        lin += campo('Modalidad', o.get('modalidad'))
        lin += campo('Contrato', o.get('contrato'))
        lin += campo('Jornada', o.get('jornada'))
        lin += campo('Salario', (o.get('salario') or 'No indicado')
                     .replace('\xa0', ' '))
        lin += campo('Publicada', (o.get('publicada') or '') +
                     (' (destacada)' if o.get('destacada') else '') +
                     '  [segun InfoJobs]')
        lin += campo('Vista 1a vez', o.get('primera_vista', ''))
        lin += campo('Enlace', o.get('enlace'))
        if o.get('resumen'):
            lin += [''] + parrafos_resumen(o['resumen'])
        lin.append('')
    nombre = f'busqueda_{slug(est["clave"], 30)}_p{est["pagina"]:02d}.txt'
    with open(nombre, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lin) + '\n')


def es_listado(url):
    """Busquedas (/jobsearch/search-results/) y listados de ofertas
    (/ofertas-trabajo/...) de InfoJobs; no una oferta suelta."""
    p = urlparse(url)
    return (p.netloc.endswith('infojobs.net')
            and not re.search(r'/of-i[0-9a-f]{20,}', p.path)
            and ('/jobsearch/search-results/' in p.path
                 or p.path.startswith('/ofertas-trabajo')))


def base_busqueda(url):
    """La URL sin el numero de pagina, para saber si es la misma busqueda."""
    p = urlparse((url or '').split('#')[0])
    q = parse_qs(p.query, keep_blank_values=True)
    q.pop('page', None)
    return (p.netloc, p.path.rstrip('/'), tuple(sorted(
        (k, tuple(v)) for k, v in q.items())))


def archivar_busqueda_anterior(est):
    """Mueve lo de la busqueda anterior a una subcarpeta, para empezar la
    nueva numerando desde 1."""
    carpeta = time.strftime('anterior_%Y%m%d_%H%M%S')
    os.makedirs(carpeta, exist_ok=True)
    movidos = 0
    for nombre in [ESTADO, DATOS] + glob.glob('busqueda_*.txt') + \
            glob.glob('oferta_*.txt'):
        if os.path.exists(nombre):
            shutil.move(nombre, os.path.join(carpeta, nombre))
            movidos += 1
    print(f'Busqueda nueva: lo de "{est.get("clave")}" se ha movido a '
          f'{carpeta}/ ({movidos} elementos).')


def cmd_buscar(url):
    url = url.strip().split('#')[0]
    if not es_listado(url):
        sys.exit('La URL debe ser una lista de ofertas de InfoJobs:\n'
                 '  - resultados de busqueda (.../jobsearch/search-results/...)\n'
                 '  - listados por categoria o lugar (.../ofertas-trabajo/...)')
    est = cargar_estado()
    if est['url'] and base_busqueda(est['url']) != base_busqueda(url):
        archivar_busqueda_anterior(est)
    abrir_y_listar(url)


def abrir_y_listar(url):
    est = cargar_estado()
    datos = esperar_datos(abrir_en_firefox(url))
    if not datos or aviso_captcha(datos):
        return
    if datos.get('tipo') != 'busqueda':
        sys.exit('La pagina abierta no es una lista de ofertas de InfoJobs.')
    if not datos.get('ofertas'):
        print('La pagina no tiene ofertas (quiza ya no hay mas resultados).')
        est['siguiente'] = False
        guardar_estado(est)
        return
    numeros = registrar_busqueda(est, datos)
    mostrar_pagina(est, numeros)


def cmd_pagina(n):
    est = cargar_estado()
    if not est['url']:
        sys.exit('Primero haz una busqueda:  python empinf.py buscar "URL"')
    if n < 1:
        sys.exit('Ya estas en la primera pagina.')
    abrir_y_listar(url_pagina(est['url'], n))


def cmd_siguiente():
    est = cargar_estado()
    if est.get('siguiente') is False:
        sys.exit('Ya estas en la ultima pagina de la busqueda.')
    cmd_pagina(int(est.get('pagina') or 0) + 1)


# ----------------------------------------------------------------------
# Ofertas
# ----------------------------------------------------------------------
def txt_oferta(n, d, previo):
    lin = ['=' * ANCHO] + textwrap.wrap(f'[{n}] {d["titulo"].upper()}', ANCHO)
    lin += ['=' * ANCHO, '']
    lin += campo('Empresa', d.get('empresa'))
    for i, x in enumerate(d.get('detalles', [])):
        xl = x.lower()
        if ':' in x:
            continue                        # ya sale en "Requisitos y datos"
        if i == 0:
            etiqueta = 'Ubicacion'
        elif 'salario' in xl or '€' in x:
            etiqueta = 'Salario'
        elif 'contrato' in xl or 'jornada' in xl:
            etiqueta = 'Contrato'
        elif any(k in xl for k in ('teletrabajo', 'presencial', 'híbrido',
                                   'hibrido', 'remoto')):
            etiqueta = 'Modalidad'
        else:
            etiqueta = 'Otros'
        lin += campo(etiqueta, x)
    lin += campo('Publicada', re.sub(r'^Publicada\s*', '',
                                     d.get('publicada') or ''))
    lin += campo('Inscritos', re.sub(r'\s*inscritos.*', '',
                                     d.get('inscritos') or ''))
    lin += campo('Compatibilidad', d.get('compatibilidad'))
    if previo and previo.get('primera_vista'):
        lin += campo('Vista 1a vez', previo['primera_vista'] +
                     '  (fecha real en que la viste por primera vez)')
    lin += campo('Enlace', d.get('url'))
    if d.get('datos'):
        lin += ['', 'REQUISITOS Y DATOS', '-' * 18]
        for k, v in d['datos']:
            lin += campo(k, v)
    if d.get('descripcion'):
        lin += ['', 'DESCRIPCION', '-' * 11] + envolver(d['descripcion'])
    elif previo and previo.get('resumen'):
        lin += ['', 'DESCRIPCION (resumen del listado)', '-' * 33] + \
            parrafos_resumen(previo['resumen'])
    return '\n'.join(lin) + '\n'


def guardar_oferta(est, datos, n=None):
    if n is None:
        n = num_de(est, datos.get('id'))
    if n is None:                               # oferta que no estaba listada
        n = len(est['ofertas']) + 1
        est['ofertas'][str(n)] = {'id': datos.get('id'),
                                  'titulo': datos.get('titulo', ''),
                                  'empresa': datos.get('empresa', ''),
                                  'enlace': datos.get('url', ''),
                                  'pagina': None}
    previo = est['ofertas'][str(n)]
    nombre = f'oferta_{n:03d}_{slug(datos.get("empresa"), 25)}_' \
             f'{slug(datos.get("titulo"), 40)}.txt'
    with open(nombre, 'w', encoding='utf-8') as f:
        f.write(txt_oferta(n, datos, previo))
    previo['fichero'] = nombre
    guardar_estado(est)
    return nombre


def cmd_abrir(numeros):
    est = cargar_estado()
    pendientes = []
    for x in numeros:
        if str(x) not in est['ofertas']:
            print(f'No hay ninguna oferta con el numero {x}. '
                  'Mira la lista con:  python empinf.py lista')
            continue
        pendientes.append(x)
    for i, n in enumerate(pendientes):
        o = est['ofertas'][str(n)]
        print(f'\n[{n}] {o["titulo"]} ({o["empresa"]})')
        ultima = (i == len(pendientes) - 1)
        datos = esperar_datos(abrir_en_firefox(o['enlace'], cerrar=True),
                              volver=ultima)
        if aviso_captcha(datos):
            restantes = pendientes[i + 1:]
            if restantes:
                print('Quedan sin abrir: ' + ' '.join(map(str, restantes)))
            return
        if datos and datos.get('tipo') == 'oferta':
            print('Guardada en: ' + guardar_oferta(est, datos, n))
        elif datos:
            print('La pagina abierta no parece una oferta; no se guarda.')
        if i < len(pendientes) - 1:
            time.sleep(ESPERA)


def cmd_ver(n):
    est = cargar_estado()
    o = est['ofertas'].get(str(n))
    if not o:
        sys.exit(f'No hay ninguna oferta con el numero {n}.')
    if o.get('fichero') and os.path.exists(o['fichero']):
        with open(o['fichero'], encoding='utf-8') as f:
            print(f.read())
        return
    print(f'(Ficha no guardada; para guardarla: python empinf.py abrir {n})\n')
    lin = [f'[{n}] {o["titulo"]}', '-' * ANCHO]
    for k in ('empresa', 'ubicacion', 'modalidad', 'contrato', 'jornada',
              'salario', 'publicada', 'enlace'):
        lin += campo(k.capitalize(), (o.get(k) or '').replace('\xa0', ' '))
    if o.get('resumen'):
        lin += [''] + parrafos_resumen(o['resumen'])
    print('\n'.join(lin))


def cmd_lista(filtro=''):
    est = cargar_estado()
    if not est['ofertas']:
        sys.exit('Todavia no hay ofertas. Empieza con:  '
                 'python empinf.py buscar "URL"')
    f = filtro.lower()
    print(f'\n"{est["clave"]}" - {len(est["ofertas"])} ofertas vistas '
          f'(ultima pagina abierta: {est["pagina"]})\n')
    if orden_elegido() != 'web':
        print(f'(orden: {orden_elegido()})')
    anchos = columnas()
    print(cabecera(anchos))
    pag = None
    calcular_interes(est)
    real = orden_elegido() != 'web'
    for n in [str(x) for x in ordenar(sorted(map(int, est['ofertas'])),
                                      est)]:
        o = est['ofertas'][n]
        buscable = ' '.join(str(o.get(k) or '') for k in (
            'titulo', 'empresa', 'ubicacion', 'modalidad', 'contrato',
            'salario', 'resumen')).lower()
        if f and f not in buscable:
            continue
        if not real and o.get('pagina') != pag:
            pag = o.get('pagina')
            print(f'  -- pagina {pag} --' if pag else '  -- otras --')
        print(fila(int(n), o, anchos))



def cmd_nuevas(args):
    """Ofertas de la busqueda actual vistas por primera vez en los
    ultimos N dias (1 = solo hoy), de la mas nueva a la mas antigua."""
    dias = int(args[0]) if args else 1
    est = cargar_estado()
    if not est['ofertas']:
        sys.exit('Todavia no hay ofertas. Empieza con:  '
                 'python empinf.py buscar "URL"')
    limite = time.strftime('%Y-%m-%d', time.localtime(
        time.time() - (dias - 1) * 86400))
    calcular_interes(est)
    nums = [int(n) for n, o in est['ofertas'].items()
            if (o.get('primera_vista') or '') >= limite]
    nums.sort(key=lambda n: (est['ofertas'][str(n)].get('primera_vista', ''),
                             -n), reverse=True)
    total = len(est['ofertas'])
    cuando = 'hoy' if dias == 1 else f'en los ultimos {dias} dias'
    print(f'\n"{est["clave"]}": {len(nums)} de {total} ofertas son nuevas '
          f'de verdad ({cuando}).')
    print('Las demas ya las habias visto antes, aunque InfoJobs las muestre '
          'como recientes.\n')
    if not nums:
        return
    anchos = columnas()
    print(cabecera(anchos))
    for n in nums:
        print(fila(n, est['ofertas'][str(n)], anchos))


def cmd_guardadas():
    est = cargar_estado()
    hay = [(int(n), o) for n, o in est['ofertas'].items() if o.get('fichero')]
    if not hay:
        sys.exit('Aun no has guardado ninguna ficha. Usa:  '
                 'python empinf.py abrir N')
    for n, o in sorted(hay):
        print(f'{n:>3}  {o["fichero"]}')


def cmd_importar():
    est = cargar_estado()
    archivos = sorted(glob.glob(os.path.join(DESCARGAS, 'empinf_nav*.json')))
    if not archivos:
        sys.exit('No hay archivos empinf_nav*.json en Descargas.')
    for ruta in archivos:
        try:
            with open(ruta, encoding='utf-8') as f:
                datos = json.load(f)
        except (ValueError, OSError):
            continue
        os.makedirs(DATOS, exist_ok=True)
        shutil.move(ruta, os.path.join(DATOS, os.path.basename(ruta)))
        if datos.get('tipo') == 'busqueda':
            nums = registrar_busqueda(est, datos)
            print(f'Busqueda "{datos.get("clave")}" pag. {datos.get("pagina")}:'
                  f' {len(nums)} ofertas')
        elif datos.get('tipo') == 'oferta':
            print('Oferta: ' + guardar_oferta(est, datos))


def cmd_estado():
    est = cargar_estado()
    if not est['url']:
        print('Sin busqueda en esta carpeta.')
        return
    print(f'Busqueda:   "{est["clave"]}"')
    print(f'Pagina:     {est["pagina"]}'
          + ('' if est.get('siguiente') is not False else ' (ultima)'))
    print(f'Total:      {est.get("total") or "?"} ofertas en InfoJobs')
    print(f'Vistas:     {len(est["ofertas"])}   Fichas guardadas: '
          f'{sum(1 for o in est["ofertas"].values() if o.get("fichero"))}')
    print(f'URL:        {est["url"]}')


# ======================================================================
# ISDEFE  (seleccionisdefe.es) - se lee directamente, sin Firefox
# ======================================================================
ISDEFE_WEB = 'https://seleccionisdefe.es'
ISDEFE_ESTADO = '.empinf_isdefe.json'
ISDEFE_LISTADO = 'isdefe_listado.txt'
NAVEGADOR = ('Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36')


def descargar_pagina(url):
    req = urllib.request.Request(url, headers={
        'User-Agent': NAVEGADOR, 'Accept-Language': 'es-ES,es;q=0.9'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            crudo = r.read()
            cod = r.headers.get_content_charset() or 'utf-8'
    except urllib.error.HTTPError as e:
        sys.exit(f'La web respondio con error {e.code} al pedir {url}')
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f'No se pudo conectar con {url}\n({e})\n'
                 'Comprueba la conexion a internet.')
    return crudo.decode(cod, errors='replace')


def _txt(fragmento):
    return re.sub(r'\s+', ' ', unescape(re.sub(r'<[^>]+>', ' ',
                                               fragmento or ''))).strip()


def isdefe_leer_listado(pagina):
    """Filas de la tabla de puestos: fecha, nombre, referencia, estado."""
    puestos, vistos = [], set()
    for fila in re.split(r'<tr\b', pagina, flags=re.I)[1:]:
        m = re.search(r'convocatoria-(\d+)', fila)
        f = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', _txt(fila))
        if not m or not f or m.group(1) in vistos:
            continue
        vistos.add(m.group(1))
        celdas = [_txt(c) for c in re.findall(r'<td\b[^>]*>(.*?)</td>',
                                              fila, re.S | re.I)]
        otros = ('perfil del puesto', 'calendario', 'inscribirse')
        candidatas = [c for c in celdas if c and not re.fullmatch(
            r'\d{1,2}/\d{1,2}/\d{4}', c) and c.lower() not in otros
            and not c.lower().startswith('cerrada')]
        nombre = max(candidatas, key=len) if candidatas else ''
        ref = re.search(r'\((\d{4}-[A-Z]\d+)\)\s*$', nombre)
        texto_fila = ' '.join(celdas).lower()
        if re.search(r'registro-\d+', fila) and 'cerrada inscrip' not in \
                texto_fila:
            estado = 'Abierta'
        elif 'cerrada inscrip' in texto_fila:
            estado = 'Cerrada'
        elif 'cerrada reclam' in texto_fila:
            estado = 'Cerrada'
        else:
            estado = ''
        dia, mes, anio = (int(x) for x in f.groups())
        puestos.append({
            'id': int(m.group(1)),
            'fecha': f'{dia:02d}/{mes:02d}/{anio}',
            'orden': f'{anio:04d}{mes:02d}{dia:02d}',
            'nombre': re.sub(r'\s*\(\d{4}-[A-Z]\d+\)\s*$', '', nombre),
            'ref': ref.group(1) if ref else '',
            'estado': estado,
            'url': f'{ISDEFE_WEB}/convocatoria-{m.group(1)}'})
    # mas reciente primero; a igual fecha, el de numero mas alto
    puestos.sort(key=lambda p: (p['orden'], p['id']), reverse=True)
    return puestos


class _TextoPerfil(HTMLParser):
    """Convierte la pagina del perfil en bloques (titulo, texto, lista)."""
    OMITIR = {'script', 'style', 'nav', 'header', 'footer', 'noscript',
              'form', 'svg'}
    BLOQUE = {'p', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'br',
              'tr', 'section', 'article', 'ul', 'ol', 'dd', 'dt'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.bloques = []           # (tipo, texto)
        self._omitir = 0
        self._tipo = 'p'
        self._buf = []

    def _cerrar(self):
        t = re.sub(r'\s+', ' ', ''.join(self._buf)).strip()
        if t:
            self.bloques.append((self._tipo, t))
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in self.OMITIR:
            self._omitir += 1
        if self._omitir:
            return
        if tag in self.BLOQUE:
            self._cerrar()
            self._tipo = {'li': 'li', 'h1': 'h1', 'h2': 'h', 'h3': 'h',
                          'h4': 'h', 'h5': 'h', 'h6': 'h'}.get(tag, 'p')

    def handle_endtag(self, tag):
        if tag in self.OMITIR:
            self._omitir = max(0, self._omitir - 1)
            return
        if not self._omitir and tag in self.BLOQUE:
            self._cerrar()
            self._tipo = 'p'

    def handle_data(self, data):
        if not self._omitir:
            self._buf.append(data)


def isdefe_leer_perfil(pagina):
    p = _TextoPerfil()
    p.feed(pagina)
    p._cerrar()
    bloques = p.bloques
    # empieza en el titulo del puesto y acaba antes del pie de pagina
    inicio = next((i for i, (t, x) in enumerate(bloques) if t == 'h1'), None)
    if inicio is None:
        inicio = next((i for i, (t, x) in enumerate(bloques)
                       if x.lower().startswith('bases para')), 0)
    fin = len(bloques)
    for i in range(inicio + 1, len(bloques)):
        if re.match(r'(aviso legal|pol[ií]tica (de )?privacidad|cookies)',
                    bloques[i][1], re.I):
            fin = i
            break
    titulo = re.sub(r'^Bases para\s+', '', bloques[inicio][1]) \
        if bloques else ''
    return titulo, bloques[inicio + 1:fin]


def isdefe_txt_perfil(n, puesto, titulo, bloques):
    lin = ['=' * ANCHO] + textwrap.wrap(f'[{n}] {titulo.upper()}', ANCHO)
    lin += ['=' * ANCHO, '']
    lin += campo('Empresa', 'ISDEFE (seleccionisdefe.es)')
    lin += campo('Referencia', puesto.get('ref'))
    lin += campo('Publicada', puesto.get('fecha'))
    lin += campo('Inscripcion', puesto.get('estado'))
    lin += campo('Perfil', puesto.get('url'))
    lin += campo('Calendario', f'{ISDEFE_WEB}/calendario-{puesto["id"]}')
    lin += campo('Inscribirse', f'{ISDEFE_WEB}/registro-{puesto["id"]}')
    lin.append('')
    for tipo, texto in bloques:
        if tipo in ('h', 'h1'):
            cab = texto.rstrip(':').upper()
            lin += ['', cab, '-' * min(len(cab), ANCHO)]
        elif tipo == 'li':
            lin += textwrap.wrap('- ' + texto, ANCHO, subsequent_indent='  ')
        else:
            lin += textwrap.wrap(texto, ANCHO)
    return '\n'.join(lin) + '\n'


def isdefe_estado():
    if os.path.exists(ISDEFE_ESTADO):
        with open(ISDEFE_ESTADO, encoding='utf-8') as f:
            return json.load(f)
    return {'puestos': [], 'actualizado': ''}


def cmd_isdefe_lista(cuantos=15):
    print('Leyendo seleccionisdefe.es/puestos ...')
    puestos = isdefe_leer_listado(descargar_pagina(ISDEFE_WEB + '/puestos'))
    if not puestos:
        sys.exit('No he encontrado puestos en la pagina. Puede que Isdefe '
                 'haya cambiado su web.')
    anteriores = {p['id']: p.get('fichero', '')
                  for p in isdefe_estado().get('puestos', [])}
    lista = puestos[:cuantos]
    for i, p in enumerate(lista, 1):
        p['n'] = i
        p['fichero'] = anteriores.get(p['id'], '')
    ahora = time.strftime('%d/%m/%Y %H:%M')
    with open(ISDEFE_ESTADO, 'w', encoding='utf-8') as f:
        json.dump({'puestos': lista, 'actualizado': ahora,
                   'total_web': len(puestos)}, f, ensure_ascii=False,
                  indent=1)

    ancho_pantalla = shutil.get_terminal_size((ANCHO, 24)).columns
    ancho_nombre = max(30, ancho_pantalla - 1 - (4 + 12 + 9 + 13))
    cab = (f'{"N":>3}  {"Publicada":<10}  {"Inscr.":<7}  {"Referencia":<11}  '
           f'Puesto')
    lin_pantalla = [f'\nISDEFE - ultimos {len(lista)} puestos publicados '
                    f'(de {len(puestos)} en la web)   {ahora}\n', cab,
                    '-' * min(ancho_pantalla - 1, 4 + 12 + 9 + 13 +
                              ancho_nombre)]
    lin_txt = [f'ISDEFE - ULTIMOS {len(lista)} PUESTOS PUBLICADOS',
               f'Consultado: {ahora}   Puestos en la web: {len(puestos)}',
               f'Fuente: {ISDEFE_WEB}/puestos', '']
    for p in lista:
        marca = '*' if p['fichero'] else ' '
        lin_pantalla.append(
            f'{p["n"]:>3}{marca} {p["fecha"]:<10}  {p["estado"] or "-":<7}  '
            f'{p["ref"] or "-":<11}  {recorta(p["nombre"], ancho_nombre)}')
        lin_txt += [f'[{p["n"]}] {p["nombre"]}']
        lin_txt += campo('    Publicada', p['fecha'])
        lin_txt += campo('    Referencia', p['ref'])
        lin_txt += campo('    Inscripcion', p['estado'])
        lin_txt += campo('    Perfil', p['url'])
        lin_txt.append('')
    with open(ISDEFE_LISTADO, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lin_txt) + '\n')
    print('\n'.join(lin_pantalla))
    print(f'\n(* = perfil ya guardado)   Guardado en: {ISDEFE_LISTADO}')
    print('Ver un puesto:  python empinf.py isdefe ver N')


def cmd_isdefe_ver(numeros):
    est = isdefe_estado()
    por_n = {p['n']: p for p in est.get('puestos', [])}
    if not por_n:
        sys.exit('Primero consulta el listado:  python empinf.py isdefe')
    for k, n in enumerate(numeros):
        p = por_n.get(n)
        if not p:
            print(f'No hay puesto numero {n} en el ultimo listado '
                  f'({est.get("actualizado")}).')
            continue
        if k:
            time.sleep(2)                       # sin prisas con la web
        print(f'Leyendo el perfil del puesto {n}...')
        titulo, bloques = isdefe_leer_perfil(descargar_pagina(p['url']))
        texto = isdefe_txt_perfil(n, p, titulo or p['nombre'], bloques)
        nombre = (f'isdefe_{n:02d}_{p["ref"] or p["id"]}_'
                  f'{slug(p["nombre"], 40)}.txt')
        with open(nombre, 'w', encoding='utf-8') as f:
            f.write(texto)
        p['fichero'] = nombre
        print(texto)
        print(f'Guardado en: {nombre}\n')
    with open(ISDEFE_ESTADO, 'w', encoding='utf-8') as f:
        json.dump(est, f, ensure_ascii=False, indent=1)


def cmd_isdefe(args):
    if args and args[0].lower() == 'ver':
        nums = [int(x) for x in re.split(r'[\s,]+', ' '.join(args[1:])) if x]
        if not nums:
            sys.exit('Indica el numero:  python empinf.py isdefe ver 3')
        cmd_isdefe_ver(nums)
    else:
        cmd_isdefe_lista(int(args[0]) if args else 15)


# ======================================================================
# INECO (ineco.epreselec.com) - via Firefox + extension (rellena el
# formulario Pais > Provincia > Oficina como lo harias tu)
# ======================================================================
INECO_URL = 'https://ineco.epreselec.com/Ofertas/Ofertas.aspx'
INECO_ESTADO = '.empinf_ineco.json'
INECO_LISTADO = 'ineco_listado.txt'
INECO_OFICINAS = {'it': 'IT', 'sistemas': 'SISTEMAS',
                  'consultoria': 'CONSULTORIA', 'consultoría': 'CONSULTORIA',
                  'civil': 'CIVIL', 'otras': 'OTRAS'}
INECO_OFI_CORTA = {'IT': 'IT', 'SISTEMAS': 'SIS', 'CONSULTORIA': 'CONS',
                   'CIVIL': 'CIV', 'OTRAS': 'OTR'}
MESES = {'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5,
         'junio': 6, 'julio': 7, 'agosto': 8, 'septiembre': 9, 'setiembre': 9,
         'octubre': 10, 'noviembre': 11, 'diciembre': 12}


def fecha_es(texto):
    """'25 de septiembre, 2026' -> '2026-09-25'."""
    m = re.search(r'(\d{1,2})\s+de\s+([a-záéíóú]+),?\s+(\d{4})',
                  (texto or '').lower())
    if not m or _norm(m.group(2)) not in MESES:
        return ''
    return f'{int(m.group(3)):04d}-{MESES[_norm(m.group(2))]:02d}-' \
           f'{int(m.group(1)):02d}'


def guardar_pdf(texto, ruta):
    """PDF sencillo en letra de ancho fijo (mantiene las columnas)."""
    try:
        from fpdf import FPDF
    except ImportError:
        print('Para crear PDF instala una vez:\n'
              '   pkg install python-pillow\n   pip install fpdf2')
        return False
    pdf = FPDF(format='A4')
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    pdf.set_font('Courier', size=8.5)
    for linea in texto.split('\n'):
        linea = linea.replace('€', 'EUR').replace('–', '-').replace(
            '—', '-').replace('ª', 'a')
        linea = linea.encode('latin-1', 'replace').decode('latin-1')
        pdf.cell(0, 3.8, linea, new_x='LMARGIN', new_y='NEXT')
    pdf.output(ruta)
    return True


# Referencias internas que Ineco pone a veces delante del puesto, p. ej.
# "TRE26-ECE-085 - ..." o "TRE26- ESS-056 - ..." (con espacios sueltos)
INECO_REF = re.compile(r'^\s*([A-Z]{2,6}\d{2,4}\s*-\s*[A-Z]{2,6}\s*-\s*\d{2,4})'
                       r'\s*[-–:]?\s*')


def ineco_partir_titulo(t):
    """'28960 - TRE26-ECE-085 - Puesto' -> ('28960', 'Puesto')."""
    m = re.match(r'\s*(\d{4,6})\s*-?\s*(.*)', t or '')
    codigo, puesto = (m.group(1), m.group(2)) if m else ('', t or '')
    return codigo, INECO_REF.sub('', puesto).strip(' -–')


def ineco_ref(t):
    """La referencia interna (TRE26-ECE-085) si la hay, normalizada."""
    m = re.match(r'\s*(?:\d{4,6}\s*-?\s*)?', t or '')
    r = INECO_REF.match((t or '')[m.end():] if m else (t or ''))
    return re.sub(r'\s+', '', r.group(1)) if r else ''


def cmd_ineco_lista(oficinas, provincia='Madrid', pais='Espana', pdf=False):
    todas = {}
    for i, ofi in enumerate(oficinas):
        ultima = (i == len(oficinas) - 1)
        print(f'\nIneco: {"España" if pais == "Espana" else pais} > '
              f'{provincia} > oficina {ofi}')
        extra = f'~p={pais}~r={provincia}~o={ofi}'
        datos = esperar_datos(abrir_en_firefox(INECO_URL, cerrar=True,
                                               extra=extra), volver=ultima)
        if not datos or aviso_captcha(datos):
            return
        if datos.get('tipo') == 'error':
            print('Error en la web de Ineco: ' + datos.get('error', '?'))
            continue
        if datos.get('tipo') != 'ineco_listado':
            print('La pagina abierta no es el listado de Ineco.')
            continue
        f = datos.get('filtro', {})
        print(f'  Filtro aplicado en la web: {f.get("pais")} > '
              f'{f.get("provincia")} > {f.get("oficina")}   '
              f'(total en la web: {datos.get("total")})')
        n = 0
        for o in datos.get('ofertas', []):
            if _norm(provincia) not in _norm(o.get('lugar')):
                continue                       # seguridad: solo la provincia
            prev = todas.setdefault(o['id'], dict(o, oficinas=[]))
            if ofi not in prev['oficinas']:
                prev['oficinas'].append(ofi)
            n += 1
        print(f'  {n} ofertas en {provincia}')
        if not ultima:
            time.sleep(6)
    lista = list(todas.values())
    for o in lista:
        o['fecha_iso'] = fecha_es(o.get('fecha'))
        o['codigo'], o['puesto'] = ineco_partir_titulo(o.get('titulo'))
    lista.sort(key=lambda o: (o['fecha_iso'], o['id']), reverse=True)
    for k, o in enumerate(lista, 1):
        o['n'] = k
    ahora = time.strftime('%d/%m/%Y %H:%M')
    guardar_json(INECO_ESTADO, {'ofertas': lista, 'actualizado': ahora,
                                'filtro': [pais, provincia, oficinas]})
    ancho = shutil.get_terminal_size((ANCHO, 24)).columns
    anp = max(30, ancho - 1 - (4 + 12 + 6))
    cab = f'{"N":>3}  {"Publicada":<10}  {"Ofi.":<4}  Puesto'
    pais_ver = 'España' if pais == 'Espana' else pais
    pant = [f'\nINECO - {pais_ver} > {provincia} > {", ".join(oficinas)}   '
            f'{len(lista)} ofertas   {ahora}\n', cab,
            '-' * min(ancho - 1, 4 + 12 + 6 + anp)]
    txt = [f'INECO - OFERTAS {pais_ver.upper()} > {provincia.upper()} > '
           f'{", ".join(oficinas)}',
           f'Consultado: {ahora}   Ofertas: {len(lista)}   '
           f'(de la mas reciente a la mas antigua)', f'Fuente: {INECO_URL}',
           '']
    for o in lista:
        fecha = (f'{o["fecha_iso"][8:10]}/{o["fecha_iso"][5:7]}/'
                 f'{o["fecha_iso"][:4]}' if o['fecha_iso'] else o['fecha'])
        ofi = (INECO_OFI_CORTA.get(o['oficinas'][0], o['oficinas'][0][:4])
               if len(o['oficinas']) == 1 else 'vari')
        pant.append(f'{o["n"]:>3}  {fecha:<10}  {ofi:<4}  '
                    f'{recorta(o["puesto"], anp)}')
        txt.append(f'[{o["n"]}] {o["puesto"]}')
        txt += campo('    Publicada', fecha)
        txt += campo('    Codigo', o['codigo'])
        txt += campo('    Referencia', ineco_ref(o.get('titulo')))
        txt += campo('    Oficina', ', '.join(o['oficinas']))
        txt += campo('    Tipo', o.get('tipo'))
        txt += campo('    Lugar', o.get('lugar'))
        txt += campo('    Enlace', f'{INECO_URL}?Id_Oferta={o["id"]}')
        txt.append('')
    with open(INECO_LISTADO, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt) + '\n')
    print('\n'.join(pant))
    print('\nOfi.: IT, SIS = Sistemas, CONS = Consultoria, vari = varias.')
    print(f'Guardado en: {INECO_LISTADO}')
    if pdf and guardar_pdf('\n'.join(txt), INECO_LISTADO[:-4] + '.pdf'):
        print(f'PDF: {INECO_LISTADO[:-4]}.pdf')
    print('Ver una oferta:  python empinf.py ineco ver N')


def ineco_txt_oferta(n, d, o):
    codigo, puesto = ineco_partir_titulo(d.get('titulo') or o.get('titulo'))
    lin = ['=' * ANCHO] + textwrap.wrap(f'[{n}] {puesto.upper()}', ANCHO)
    lin += ['=' * ANCHO, '']
    lin += campo('Empresa', 'INECO (ineco.epreselec.com)')
    lin += campo('Codigo', codigo)
    lin += campo('Referencia', ineco_ref(d.get('titulo') or o.get('titulo')))
    lin += campo('Publicada', o.get('fecha'))
    lin += campo('Oficina', d.get('oficina'))
    lin += campo('Tipo de oferta', d.get('tipo_oferta'))
    lin += campo('Provincia', d.get('provincia'))
    lin += campo('Pais', d.get('pais'))
    lin += campo('Vacantes', d.get('vacantes'))
    lin += campo('Enlace', d.get('url'))
    for titulo, cuerpo in d.get('secciones', []):
        cab = titulo.upper()
        lin += ['', cab, '-' * len(cab)] + envolver(cuerpo)
    return '\n'.join(lin) + '\n'


def cmd_ineco_ver(numeros, pdf=False):
    est = cargar_json(INECO_ESTADO, {'ofertas': []})
    por_n = {o['n']: o for o in est['ofertas']}
    if not por_n:
        sys.exit('Primero consulta el listado:  python empinf.py ineco')
    for k, n in enumerate(numeros):
        o = por_n.get(n)
        if not o:
            print(f'No hay oferta numero {n} en el ultimo listado.')
            continue
        if k:
            time.sleep(ESPERA)
        print(f'\n[{n}] {o.get("puesto") or o.get("titulo")}')
        datos = esperar_datos(abrir_en_firefox(
            f'{INECO_URL}?Id_Oferta={o["id"]}', cerrar=True),
            volver=(k == len(numeros) - 1))
        if not datos or aviso_captcha(datos):
            return
        if datos.get('tipo') != 'ineco_oferta':
            print('La pagina abierta no parece una oferta de Ineco.')
            continue
        texto = ineco_txt_oferta(n, datos, o)
        nombre = f'ineco_{n:02d}_{o.get("codigo") or o["id"]}_' \
                 f'{slug(o.get("puesto"), 40)}.txt'
        with open(nombre, 'w', encoding='utf-8') as f:
            f.write(texto)
        print(texto)
        print(f'Guardado en: {nombre}')
        if pdf and guardar_pdf(texto, nombre[:-4] + '.pdf'):
            print(f'PDF: {nombre[:-4]}.pdf')


def cmd_ineco(args):
    args = [a.lower() for a in args]
    pdf = 'pdf' in args
    args = [a for a in args if a != 'pdf']
    if args and args[0] == 'ver':
        nums = [int(x) for x in re.split(r'[\s,]+', ' '.join(args[1:])) if x]
        if not nums:
            sys.exit('Indica el numero:  python empinf.py ineco ver 3')
        return cmd_ineco_ver(nums, pdf)
    provincia = 'Madrid'
    oficinas = []
    for a in args:
        if a in INECO_OFICINAS:
            oficinas.append(INECO_OFICINAS[a])
        elif a.startswith('provincia='):
            provincia = a.split('=', 1)[1].capitalize()
        else:
            sys.exit(f'No entiendo "{a}". Oficinas: it, sistemas, '
                     'consultoria, civil, otras')
    cmd_ineco_lista(oficinas or ['IT', 'SISTEMAS', 'CONSULTORIA'],
                    provincia, pdf=pdf)


# ======================================================================
# LINKEDIN EMPLEOS - via Firefox + extension (tu sesion de LinkedIn)
# ======================================================================
LK_ESTADO = '.empinf_linkedin.json'
LK_INICIO = 'https://www.linkedin.com/jobs/'
LK_REGISTRO = 'https://www.linkedin.com/jobs-tracker/'
LK_PERSPECTIVAS = 'https://www.linkedin.com/explore-career-insights/'
LK_PESTANAS = {'guardados': 'Guardados', 'en curso': 'En curso',
               'encurso': 'En curso', 'solicitados': 'Solicitados',
               'entrevistas': 'Entrevistas', 'archivados': 'Archivados'}


def lk_estado():
    return cargar_json(LK_ESTADO, {'ofertas': {}, 'vista': '', 'lote': 0})


def lk_lote():
    return int(cargar_json(CONFIG, {}).get('linkedin_lote', 10))


def lk_abrir(url, modo, extra='', volver=True):
    datos = esperar_datos(abrir_en_firefox(
        url, cerrar=True, extra=f'~m={modo}' + extra), volver=volver)
    if not datos:
        return None
    if datos.get('tipo', '').startswith('linkedin'):
        return datos
    print('La pagina abierta no parece de LinkedIn Empleos (¿sesion '
          'iniciada en Firefox?).')
    return None


def lk_registrar(est, ofertas, vista):
    """Numera las ofertas (sin repetir) y apunta la 1a vez que se ven."""
    for o in ofertas:
        o['modalidad'] = o.get('modalidad') or ''
        o['hid'] = 'lk' + o['id']
    copia = [dict(o, id=o['hid']) for o in ofertas]
    anotar_historial(copia)
    nums = []
    por_id = {o['id']: int(n) for n, o in est['ofertas'].items()}
    for o, c in zip(ofertas, copia):
        o['primera_vista'] = c.get('primera_vista', '')
        n = por_id.get(o['id'])
        if n is None:
            n = len(est['ofertas']) + 1
            por_id[o['id']] = n
            o['fichero'] = ''
            est['ofertas'][str(n)] = o
        else:
            viejo = est['ofertas'][str(n)]
            o['fichero'] = viejo.get('fichero', '')
            est['ofertas'][str(n)] = dict(viejo, **o)
        nums.append(n)
    est['vista'] = vista
    guardar_json(LK_ESTADO, est)
    return nums


def lk_hace(t):
    """'hace 1 semana' -> '1 sem', '1 hace un año' -> '1 año'."""
    t = (t or '').replace('Compartido ', '').replace('Publicado ', '')
    t2 = re.sub(r'\b(hace|un|una)\b', ' ', t, flags=re.I)
    m = re.search(r'(\d+)\s*(minuto|hora|d[ií]a|semana|mes|a[nñ]o)', t2, re.I)
    if not m:
        return t or '-'
    u = {'minuto': 'min', 'hora': 'h', 'dia': 'd', 'día': 'd', 'semana': 'sem',
         'mes': 'mes', 'año': 'año', 'ano': 'año'}[m.group(2).lower()]
    n = int(m.group(1))
    if u == 'mes' and n > 1:
        u = 'meses'
    if u == 'año' and n > 1:
        u = 'años'
    return f'{n} {u}'


def lk_mostrar(est, nums, titulo, fichero, pdf=False, todos=None):
    ancho = shutil.get_terminal_size((ANCHO, 24)).columns
    fijo = 5 + 8 + 6 + 7 + 2
    emp = 18 if ancho - fijo > 60 else 12
    anp = max(24, ancho - 1 - fijo - emp - 2)
    cab = (f'{"N":>3}  {"Public.":<7} {"Mod.":<5} {"1ªvez":<6} '
           f'{"Empresa":<{emp}}  Puesto')
    pant = [f'\n{titulo}\n', cab, '-' * min(ancho - 1, fijo + emp + 2 + anp)]
    txt = [titulo.upper(), f'Consultado: {time.strftime("%d/%m/%Y %H:%M")}',
           '']
    seccion = None
    for n in nums:
        o = est['ofertas'][str(n)]
        if o.get('seccion') and o['seccion'] != seccion:
            seccion = o['seccion']
            pant.append(f'  -- {seccion} --')
        mod = modalidad_corta(o.get('modalidad')) or '-'
        marca = '*' if o.get('fichero') else ' '
        pub = lk_hace(o.get('publicada'))
        extra = ' [promo]' if 'promocionado' in o.get('marcas', []) else ''
        pant.append(f'{n:>3}{marca} {recorta(pub, 7):<7} {mod:<5} '
                    f'{visto_corto(o.get("primera_vista")):<6} '
                    f'{recorta(o.get("empresa"), emp):<{emp}}  '
                    f'{recorta(o.get("titulo", "") + extra, anp)}')
    seccion = None
    for n in (todos or nums):
        o = est['ofertas'][str(n)]
        if o.get('seccion') and o['seccion'] != seccion:
            seccion = o['seccion']
            txt += ['', f'--- {seccion.upper()} ---', '']
        txt.append(f'[{n}] {o.get("titulo")}')
        txt += campo('    Empresa', o.get('empresa'))
        txt += campo('    Lugar', o.get('ubicacion'))
        txt += campo('    Publicado', o.get('publicada'))
        txt += campo('    Vista 1a vez', o.get('primera_vista'))
        txt += campo('    Tu red', o.get('red'))
        txt += campo('    Detalles', ', '.join(o.get('marcas', [])))
        txt += campo('    Enlace', f'https://www.linkedin.com/jobs/view/'
                     f'{o["id"]}/')
        txt.append('')
    with open(fichero, 'w', encoding='utf-8') as f:
        f.write('\n'.join(txt) + '\n')
    print('\n'.join(pant))
    print(f'\n(* = ficha guardada, [promo] = anuncio promocionado)   '
          f'Guardado en: {fichero}')
    if pdf and guardar_pdf('\n'.join(txt), fichero[:-4] + '.pdf'):
        print(f'PDF: {fichero[:-4]}.pdf')


def lk_apartados(est):
    """Busquedas recientes y empresas que contratan (pantalla + txt)."""
    rec = est.get('recientes', [])
    emp = est.get('empresas', [])
    lin = []
    if rec:
        print('\n  -- Busquedas de empleo recientes --')
        lin += ['', '--- BUSQUEDAS DE EMPLEO RECIENTES ---', '']
        for i, r in enumerate(rec, 1):
            print(f'  R{i}  {r.get("texto")}  ({r.get("lugar")})')
            lin += [f'[R{i}] {r.get("texto")}  ({r.get("lugar")})',
                    f'      {r.get("url")}']
        print('  Abrir una:  python empinf.py linkedin reciente 1')
    if emp:
        print('\n  -- Descubre empresas que contratan profesionales con tus '
              'aptitudes --')
        lin += ['', '--- EMPRESAS QUE CONTRATAN PROFESIONALES CON TUS '
                'APTITUDES ---', '']
        for e in emp:
            print(f'  {recorta(e.get("nombre"), 30):<30}  '
                  f'{recorta(e.get("info"), 40):<40}  {e.get("estado", "")}')
            lin.append(e.get('nombre', '').upper())
            lin += campo('    Tamano/lugar', e.get('info'))
            lin += campo('    Situacion', e.get('estado'))
            lin += campo('    Lema', e.get('lema'))
            lin += campo('    Descripcion', e.get('descripcion'))
            lin += campo('    Enlace', e.get('url'))
            lin.append('')
    if lin:
        with open('linkedin_empleos.txt', 'a', encoding='utf-8') as f:
            f.write('\n'.join(lin) + '\n')


def cmd_lk_inicio(pdf=False):
    """Pagina principal de Empleos: 'coinciden con tu perfil' completa y
    'Mas empleos para ti' (que no se acaba nunca) de N en N."""
    lote = lk_lote()
    pedidas = lote
    ya = 0
    est = lk_estado()
    est['ofertas'] = {}
    while True:
        datos = lk_abrir(LK_INICIO, 'inicio', f'~n={pedidas}')
        if not datos:
            return
        if datos.get('mostrar_todo'):
            est['mostrar_todo'] = datos['mostrar_todo']
        if ya == 0:
            est['recientes'] = datos.get('recientes', [])
            est['empresas'] = datos.get('empresas', [])
        ofs = datos.get('ofertas', [])
        perfil = [o for o in ofs if 'mas empleos' not in _norm(o['seccion'])]
        mas = [o for o in ofs if 'mas empleos' in _norm(o['seccion'])]
        nuevos = mas[ya:pedidas]
        mostrar = (perfil if ya == 0 else []) + nuevos
        nums = lk_registrar(est, mostrar, 'inicio')
        titulo = (f'LINKEDIN - EMPLEOS PARA TI   (mas empleos '
                  f'{ya + 1}-{ya + len(nuevos)})')
        lk_mostrar(est, nums, titulo, 'linkedin_empleos.txt', pdf,
                   todos=sorted(int(k) for k in est['ofertas']))
        if ya == 0:
            lk_apartados(est)
        if len(mas) <= ya:
            print('LinkedIn no ha cargado mas empleos.')
            break
        ya += len(nuevos)
        try:
            r = input(f'\n¿Ver {lote} mas? [Enter = si, un numero = cuantos, '
                      f'n = no]: ').strip().lower()
        except EOFError:
            break
        if r in ('n', 'no', 'q'):
            break
        pedidas = ya + (int(r) if r.isdigit() and int(r) > 0 else lote)
        time.sleep(3)
    print('Ver una oferta:  python empinf.py linkedin ver N')


def cmd_lk_buscar(url, pagina=1, pdf=False):
    est = lk_estado()
    if url != est.get('url_busqueda'):
        est['ofertas'] = {}
    est['url_busqueda'] = url
    est['pagina'] = pagina
    datos = lk_abrir(url.split('#')[0], 'buscar', f'~pg={pagina}')
    if not datos:
        return
    nums = lk_registrar(est, datos.get('ofertas', []), 'buscar')
    total = datos.get('total') or ''
    lk_mostrar(est, nums, f'LINKEDIN - BUSQUEDA, pagina {pagina}   {total}',
               f'linkedin_busqueda_p{pagina:02d}.txt', pdf)
    print('Siguiente pagina:  python empinf.py linkedin siguiente')


def cmd_lk_registro(pestana='', pdf=False):
    est = lk_estado()
    est['ofertas'] = {}
    extra = f'~t={quote(pestana)}' if pestana else ''
    datos = lk_abrir(LK_REGISTRO, 'registro', extra)
    if not datos:
        return
    nums = lk_registrar(est, datos.get('ofertas', []), 'registro')
    print('Pestanas: ' + '   '.join(datos.get('pestanas', [])))
    nombre = slug(datos.get('activa') or pestana or 'registro', 20)
    lk_mostrar(est, nums, f'LINKEDIN - REGISTRO DE EMPLEOS: '
               f'{datos.get("activa") or "?"}',
               f'linkedin_registro_{nombre}.txt', pdf)


def cmd_lk_perspectivas(pdf=False):
    datos = lk_abrir(LK_PERSPECTIVAS, 'perspectivas')
    if not datos:
        return
    lineas = [l.rstrip() for l in (datos.get('texto') or '').split('\n')]
    texto = '\n'.join(['LINKEDIN - MIS PERSPECTIVAS PROFESIONALES',
                       f'Consultado: {time.strftime("%d/%m/%Y %H:%M")}', ''] +
                      [x for l in lineas for x in (textwrap.wrap(l, ANCHO)
                                                   or [''])])
    texto = re.sub(r'\n{3,}', '\n\n', texto) + '\n'
    with open('linkedin_perspectivas.txt', 'w', encoding='utf-8') as f:
        f.write(texto)
    print(texto)
    print('Guardado en: linkedin_perspectivas.txt')
    if pdf and guardar_pdf(texto, 'linkedin_perspectivas.pdf'):
        print('PDF: linkedin_perspectivas.pdf')


def lk_txt_oferta(n, d, o):
    lin = ['=' * ANCHO] + textwrap.wrap(
        f'[{n}] {(d.get("titulo") or o.get("titulo", "")).upper()}', ANCHO)
    lin += ['=' * ANCHO, '']
    lin += campo('Empresa', d.get('empresa') or o.get('empresa'))
    lin += campo('Lugar', o.get('ubicacion'))
    lin += campo('Publicado', o.get('publicada'))
    lin += campo('Vista 1a vez', o.get('primera_vista'))
    lin += campo('Tu red', o.get('red'))
    lin += campo('Detalles', ', '.join(o.get('marcas', [])))
    for c in d.get('cabecera', []):
        lin += campo('Info', c)
    lin += campo('Enlace', f'https://www.linkedin.com/jobs/view/{o["id"]}/')
    if d.get('descripcion'):
        lin += ['', 'ACERCA DEL EMPLEO', '-' * 17] + envolver(d['descripcion'])
    if d.get('empresa_info'):
        info = re.sub(r'\n{2,}', '\n', d['empresa_info'])
        lin += ['', 'ACERCA DE LA EMPRESA', '-' * 20] + envolver(info)
    return '\n'.join(lin) + '\n'


def cmd_lk_ver(numeros, pdf=False):
    est = lk_estado()
    if not est['ofertas']:
        sys.exit('Primero consulta un listado:  python empinf.py linkedin')
    for k, n in enumerate(numeros):
        o = est['ofertas'].get(str(n))
        if not o:
            print(f'No hay oferta numero {n} en el ultimo listado.')
            continue
        if k:
            time.sleep(ESPERA)
        print(f'\n[{n}] {o.get("titulo")} ({o.get("empresa")})')
        url = o.get('enlace') or \
            f'https://www.linkedin.com/jobs/search-results/?currentJobId={o["id"]}'
        d = lk_abrir(url, 'oferta', f'~id={o["id"]}',
                     volver=(k == len(numeros) - 1))
        if not d or d.get('tipo') != 'linkedin_oferta':
            continue
        texto = lk_txt_oferta(n, d, o)
        nombre = f'linkedin_{n:03d}_{slug(o.get("empresa"), 20)}_' \
                 f'{slug(o.get("titulo"), 35)}.txt'
        with open(nombre, 'w', encoding='utf-8') as f:
            f.write(texto)
        o['fichero'] = nombre
        guardar_json(LK_ESTADO, est)
        print(texto)
        print(f'Guardado en: {nombre}')
        if pdf and guardar_pdf(texto, nombre[:-4] + '.pdf'):
            print(f'PDF: {nombre[:-4]}.pdf')


def cmd_linkedin(args):
    pdf = any(a.lower() == 'pdf' for a in args)
    args = [a for a in args if a.lower() != 'pdf']
    sub = args[0].lower() if args else 'inicio'
    resto = args[1:]
    if sub in ('inicio', 'empleos'):
        cmd_lk_inicio(pdf)
    elif sub == 'lote':
        cfg = cargar_json(CONFIG, {})
        if resto and resto[0].isdigit():
            cfg['linkedin_lote'] = max(1, int(resto[0]))
            guardar_json(CONFIG, cfg)
        print(f'"Mas empleos para ti" se muestran de '
              f'{cfg.get("linkedin_lote", 10)} en {cfg.get("linkedin_lote", 10)}.')
    elif sub == 'buscar' and resto:
        cmd_lk_buscar(resto[0], 1, pdf)
    elif sub == 'coinciden':
        enlaces = lk_estado().get('mostrar_todo', {})
        url = next((u for s, u in enlaces.items() if 'coincid' in _norm(s)),
                   None)
        if not url:
            sys.exit('Primero abre la pagina principal:  '
                     'python empinf.py linkedin')
        # El enlace "Mostrar todo" redirige (y a veces falla); se usa
        # directamente la direccion final a la que redirige LinkedIn.
        url = url.replace('origin=JobSearchOrigin_QUALIFICATION_LANDING',
                          'origin=QUALIFICATION_LANDING')
        cmd_lk_buscar(url, 1, pdf)
    elif sub in ('reciente', 'recientes'):
        rec = lk_estado().get('recientes', [])
        if not rec:
            sys.exit('No hay busquedas recientes guardadas. Abre antes:  '
                     'python empinf.py linkedin')
        if not resto:
            for i, r in enumerate(rec, 1):
                print(f'R{i}  {r.get("texto")}  ({r.get("lugar")})')
            return
        k = int(resto[0].lstrip('rR'))
        if not 1 <= k <= len(rec):
            sys.exit(f'Hay {len(rec)} busquedas recientes.')
        cmd_lk_buscar(rec[k - 1]['url'], 1, pdf)
    elif sub in ('siguiente', 'pagina'):
        est = lk_estado()
        if not est.get('url_busqueda'):
            sys.exit('Primero haz una busqueda:  python empinf.py linkedin '
                     'buscar "URL"   (o  linkedin coinciden)')
        pag = int(resto[0]) if sub == 'pagina' and resto else \
            int(est.get('pagina') or 1) + 1
        cmd_lk_buscar(est['url_busqueda'], pag, pdf)
    elif sub == 'registro':
        pest = ' '.join(resto).lower()
        cmd_lk_registro(LK_PESTANAS.get(pest, pest.capitalize()), pdf)
    elif sub in ('perspectivas', 'perspectiva'):
        cmd_lk_perspectivas(pdf)
    elif sub == 'ver' and resto:
        cmd_lk_ver([int(x) for x in re.split(r'[\s,]+', ' '.join(resto))
                    if x], pdf)
    else:
        print('Ordenes de LinkedIn: linkedin | linkedin lote N | linkedin '
              'coinciden | linkedin buscar "URL" | linkedin siguiente | '
              'linkedin pagina N | linkedin registro [pestana] | linkedin '
              'perspectivas | linkedin ver N   (+ "pdf")')


# ----------------------------------------------------------------------
def main():
    a = sys.argv[1:]
    if not a or a[0] in ('-h', '--help', 'ayuda'):
        print(__doc__)
        return
    orden, resto = a[0].lower(), a[1:]
    try:
        if orden == 'buscar' and resto:
            cmd_buscar(resto[0])
        elif orden == 'siguiente':
            cmd_siguiente()
        elif orden == 'anterior':
            cmd_pagina(int(cargar_estado().get('pagina') or 1) - 1)
        elif orden == 'pagina' and resto:
            cmd_pagina(int(resto[0]))
        elif orden == 'lista':
            cmd_lista(' '.join(resto))
        elif orden == 'abrir' and resto:
            cmd_abrir([int(x) for x in re.split(r'[\s,]+', ' '.join(resto))
                       if x])
        elif orden == 'ver' and resto:
            cmd_ver(int(resto[0]))
        elif orden == 'guardadas':
            cmd_guardadas()
        elif orden == 'importar':
            cmd_importar()
        elif orden == 'estado':
            cmd_estado()
        elif orden in ('diagnostico', 'diagnóstico'):
            cmd_diagnostico()
        elif orden == 'isdefe':
            cmd_isdefe(resto)
        elif orden == 'ineco':
            cmd_ineco(resto)
        elif orden == 'linkedin':
            cmd_linkedin(resto)
        elif orden == 'orden':
            cmd_orden(resto)
        elif orden == 'nuevas':
            cmd_nuevas(resto)
        elif orden in ('si', 'sí', 'no', 'quitar') and resto:
            nums = [int(x) for x in re.split(r'[\s,]+', ' '.join(resto)) if x]
            cmd_marcar(nums, {'si': 1, 'sí': 1, 'no': 0}.get(orden))
        elif orden in ('porque', 'porqué') and resto:
            cmd_porque(int(resto[0]))
        elif orden == 'reglas':
            cmd_reglas()
        elif orden == 'aprendizaje':
            cmd_aprendizaje()
        else:
            print(__doc__)
    except ValueError:
        sys.exit('Los numeros de oferta o de pagina deben ser numeros.')
    except KeyboardInterrupt:
        print('\nInterrumpido.')


if __name__ == '__main__':
    main()
