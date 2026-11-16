#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
queja.py - Presenta una queja/escrito en la Sede Electronica del Ministerio de
Transportes ("Presentacion de solicitudes, comunicaciones y escritos no
regulados por un procedimiento estandar") desde Termux, controlando Google
Chrome para Android del MISMO tablet por depuracion inalambrica (adb) y el
protocolo DevTools de Chrome.

Solo usa la libreria estandar de Python 3. Necesita en Termux:
    pkg install python android-tools

Que hace solo:  abrir la sede -> pulsar "Seleccionar certificado" -> (elegir el
certificado: automatico por adb o manual) -> rellenar "Datos del solicitante y
del titular" -> rellenar "Datos de la solicitud" (asunto, destinatario, expongo,
solicito, adjuntos) -> llegar a la pantalla de Confirmacion.

Que NO hace: el paso final de firma/presentacion. Se queda en la pantalla de
Confirmacion para que lo revises y lo pulses tu.

Comandos:
  plantilla [FICHERO]        crea un fichero de datos de ejemplo (JSON)
  adb-emparejar              emparejar Termux con la depuracion inalambrica
  adb-conectar PUERTO        conectar y preparar el acceso a Chrome
  estado                     comprobar adb, Chrome y la pagina actual
  presentar DATOS.json       ejecutar todo el proceso
  ui                         mostrar los botones visibles en pantalla (diagnostico)
"""
import argparse
import base64
import json
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

SEDE = os.environ.get("QUEJA_SEDE", "https://sede.transportes.gob.es")
URL_INSTANCIA = SEDE + ("/proc-servicios-comunes/instancia-general/"
                        "presentacion-solicitudes-comunicaciones-escritos-no-regulados-un-procedimiento-estandar")
URL_INICIO = SEDE + "/Procedimiento/?procedurekey=1"
CDP_PORT = int(os.environ.get("QUEJA_CDP_PORT", "9222"))
CHROME_PKG = os.environ.get("QUEJA_CHROME_PKG", "com.android.chrome")
CHROME_SOCKET = os.environ.get("QUEJA_CHROME_SOCKET", "chrome_devtools_remote")

PLANTILLA = {
    "_ayuda": "Edita los textos. 'expongo' y 'solicito' pueden ser '@fichero.txt' para leerlos de un archivo.",
    "provincia": "Ávila",
    "municipio": "Adrada, La",
    "direccion": "Travesía Juego de la Bola, 2, Bloque B, Bajo B",
    "codigo_postal": "05430",
    "telefono": "673085119",
    "correo": "jose.zuccoli@gmx.es",
    "correo_notificaciones": "jose.zuccoli@gmx.es",
    "notificacion": "electronica",
    "asunto": "Queja por ... (resumen breve)",
    "destinatario": "Dirección General de ... (unidad a la que va dirigida)",
    "expongo": "Primero.- ...\nSegundo.- ...",
    "solicito": "Que se admita este escrito y se ...",
    "adjuntos": [
        {"ruta": "/sdcard/Download/prueba.pdf", "descripcion": "Descripción del documento"}
    ],
}


# ----------------------------------------------------------------------------
# utilidades
# ----------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFD", str(s or ""))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.split()).upper()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def die(msg, code=1):
    print("ERROR: %s" % msg, file=sys.stderr)
    sys.exit(code)


# ----------------------------------------------------------------------------
# ADB
# ----------------------------------------------------------------------------
class Adb:
    def __init__(self):
        self.exe = shutil.which("adb")
        if not self.exe:
            die("No hay 'adb'. Instala:  pkg install android-tools")
        self.serial = None

    def run(self, *args, timeout=30, serial=True, check=False):
        cmd = [self.exe]
        if serial and self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "tiempo agotado: %s" % " ".join(cmd)
        return r.returncode, r.stdout

    def devices(self):
        rc, out = self.run("devices", serial=False)
        res = []
        for line in out.splitlines()[1:]:
            p = line.split()
            if len(p) >= 2:
                res.append((p[0], p[1]))
        return res

    def attach(self):
        self.run("start-server", serial=False)
        devs = [d for d in self.devices() if d[1] == "device"]
        if not devs:
            raw = self.devices()
            if any(s == "unauthorized" for _, s in raw):
                die("El tablet no ha autorizado a adb. Acepta el aviso o repite 'adb-emparejar'.")
            die("adb no esta conectado. Haz 'queja.py adb-emparejar' y 'queja.py adb-conectar PUERTO' "
                "(Ajustes > Opciones de desarrollador > Depuracion inalambrica).")
        self.serial = devs[0][0]
        return self.serial

    def forward(self):
        rc, out = self.run("forward", "tcp:%d" % CDP_PORT, "localabstract:%s" % CHROME_SOCKET)
        if rc != 0:
            die("No pude preparar el acceso a Chrome: %s" % out.strip())

    def abrir_url(self, url):
        rc, out = self.run("shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", CHROME_PKG)
        return rc == 0 and "Error" not in out

    def traer_chrome(self):
        self.run("shell", "monkey", "-p", CHROME_PKG, "-c", "android.intent.category.LAUNCHER", "1")

    def tap(self, x, y):
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))

    def ui_dump(self):
        path = "/sdcard/queja_ui.xml"
        rc, out = self.run("shell", "uiautomator", "dump", path, timeout=40)
        rc, xml = self.run("shell", "cat", path)
        return xml if "<hierarchy" in xml else ""


# ----------------------------------------------------------------------------
# Interfaz de pantalla (uiautomator) para el cuadro de certificado
# ----------------------------------------------------------------------------
BOTONES_OK = ["seleccionar", "select", "permitir", "allow", "aceptar", "accept", "ok"]
BOTONES_NO = ["denegar", "deny", "cancelar", "cancel", "rechazar", "no permitir"]
PALABRAS_CERT = ["certificad", "certificate", "credencial"]


def parse_nodes(xml):
    nodes = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return nodes
    for n in root.iter("node"):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        nodes.append({
            "text": (n.get("text") or n.get("content-desc") or "").strip(),
            "class": n.get("class", ""),
            "pkg": n.get("package", ""),
            "id": n.get("resource-id", ""),
            "clickable": n.get("clickable") == "true",
            "checkable": n.get("checkable") == "true",
            "checked": n.get("checked") == "true",
            "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
        })
    return nodes


def plan_certificado(nodes, boton_extra=None):
    """Devuelve la lista de pulsaciones [(x, y, motivo)] para el cuadro de certificado, o []."""
    texto = " ".join(n["text"].lower() for n in nodes)
    es_cert = any(p in texto for p in PALABRAS_CERT) or any("keychain" in n["pkg"].lower() for n in nodes)
    if not es_cert:
        return []
    ok_words = ([boton_extra.lower()] if boton_extra else []) + BOTONES_OK
    boton = None
    for w in ok_words:
        for n in nodes:
            t = n["text"].lower()
            # los botones de paginas web no tienen resource-id: solo se pulsan botones de dialogos
            if t == w and n["clickable"] and n["id"] and not any(x in t for x in BOTONES_NO):
                boton = n
                break
        if boton:
            break
    if not boton:
        return []
    acciones = []
    # si hay una lista con un elemento sin marcar, marcarlo primero
    cands = [n for n in nodes if n["checkable"] and not n["checked"]]
    marcados = [n for n in nodes if n["checkable"] and n["checked"]]
    if cands and not marcados:
        acciones.append((cands[0]["cx"], cands[0]["cy"], "elegir certificado"))
    acciones.append((boton["cx"], boton["cy"], "boton '%s'" % boton["text"]))
    return acciones


# ----------------------------------------------------------------------------
# Cliente WebSocket minimo (RFC 6455) + DevTools
# ----------------------------------------------------------------------------
class WS:
    def __init__(self, url, timeout=30):
        m = re.match(r"ws://([^/:]+):(\d+)(/.*)", url)
        if not m:
            raise ValueError("url ws no valida: %s" % url)
        host, port, path = m.group(1), int(m.group(2)), m.group(3)
        self.s = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (path, host, port, key)
        self.s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.s.recv(4096)
            if not chunk:
                raise ConnectionError("el servidor cerro la conexion")
            buf += chunk
        head, self.rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise ConnectionError("handshake rechazado: %s" % head.split(b"\r\n")[0].decode(errors="replace"))

    def _read(self, n):
        out = b""
        while len(out) < n:
            if self.rest:
                take, self.rest = self.rest[:n - len(out)], self.rest[n - len(out):]
                out += take
                continue
            chunk = self.s.recv(65536)
            if not chunk:
                raise ConnectionError("conexion cerrada")
            self.rest = chunk
        return out

    def send(self, text):
        data = text.encode("utf-8")
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data)) if n < 4096 else _mask_big(data, mask)
        self.s.sendall(bytes(hdr) + masked)

    def recv(self, timeout=None):
        if timeout is not None:
            self.s.settimeout(timeout)
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op = b1 & 0x0F
            fin = b1 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if b2 & 0x80:
                mask = self._read(4)
                payload = _mask_big(self._read(n), mask)
            else:
                payload = self._read(n)
            if op == 0x9:      # ping
                self._pong(payload)
                continue
            if op == 0x8:
                raise ConnectionError("Chrome cerro la conexion DevTools")
            msg += payload
            if fin:
                return msg.decode("utf-8", errors="replace")

    def _pong(self, payload):
        mask = os.urandom(4)
        self.s.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + _mask_big(payload, mask))

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def _mask_big(data, mask):
    if not data:
        return b""
    m = (mask * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(len(data), "big")


class Chrome:
    def __init__(self):
        self.ws = None
        self.mid = 0
        self.tab = None

    def tabs(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json" % CDP_PORT, timeout=5) as r:
                return [t for t in json.load(r) if t.get("type") == "page"]
        except Exception as e:
            raise ConnectionError("no hay acceso a Chrome (%s)" % e)

    def attach(self, preferir=None):
        preferir = preferir or SEDE.split("//", 1)[1]
        tabs = self.tabs()
        if not tabs:
            raise ConnectionError("Chrome no tiene pestanas abiertas")
        tab = next((t for t in tabs if preferir in t.get("url", "")), tabs[0])
        if self.ws:
            self.ws.close()
        self.ws = WS(tab["webSocketDebuggerUrl"].replace("localhost", "127.0.0.1"))
        self.tab = tab
        return tab

    def call(self, method, params=None, timeout=60):
        self.mid += 1
        mid = self.mid
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv(timeout=max(1, end - time.time())))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"].get("message")))
                return msg.get("result", {})
        raise TimeoutError(method)

    def eval(self, js, timeout=60):
        r = self.call("Runtime.evaluate", {
            "expression": js, "awaitPromise": True, "returnByValue": True, "userGesture": True}, timeout)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            raise RuntimeError("JS: %s" % (d.get("exception", {}).get("description") or d.get("text")))
        return r.get("result", {}).get("value")

    def safe_eval(self, js, timeout=30):
        """Evalua tolerando recargas de pagina (reconecta si hace falta)."""
        for _ in range(3):
            try:
                return self.eval(js, timeout)
            except (ConnectionError, OSError, BrokenPipeError, TimeoutError):
                time.sleep(1)
                try:
                    self.attach()
                except Exception:
                    pass
            except RuntimeError as e:
                if "context" in str(e).lower() or "destroyed" in str(e).lower():
                    time.sleep(1)
                    continue
                raise
        return None


# ----------------------------------------------------------------------------
# JavaScript que se ejecuta en la pagina
# ----------------------------------------------------------------------------
JS_COMUN = r"""
const sleep = ms => new Promise(r => setTimeout(r, ms));
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const norm = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim().toUpperCase();
function setVal(el, v) {
  const proto = Object.getPrototypeOf(el);
  const d = Object.getOwnPropertyDescriptor(proto, 'value');
  if (d && d.set) d.set.call(el, v); else el.value = v;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new Event('blur', {bubbles: true}));
}
function findOpt(sel, wanted) {
  return $$('option', sel).find(o => !o.disabled && o.value !== '' &&
    (o.value === String(wanted) || norm(o.textContent) === norm(wanted) || norm(o.title) === norm(wanted)));
}
"""

JS_ESTADO = r"""
(() => {
  const $ = s => document.querySelector(s);
  const p = location.pathname;
  let paso = 'otro';
  if ($('#SignButton') || /AFirmaMFOMPasarela/i.test(p)) paso = 'cert';
  else if ($('#pg_m1_gr2_provincia_titular')) paso = 'form1';
  else if ($('#pg_m2_gr1_asunto')) paso = 'form2';
  else if (/instancia-general/i.test(p)) paso = 'instancia';
  else if (/^\/Procedimiento\//i.test(p) && $('#mainForm')) paso = 'confirmacion';
  else if (/^\/Procedimiento\//i.test(p)) paso = 'procedimiento';
  const err = Array.from(document.querySelectorAll('.field-validation-error, .text-danger, .invalid-feedback, .alert-danger, .validation-summary-errors'))
    .filter(e => e.offsetParent !== null).map(e => e.textContent.replace(/\s+/g, ' ').trim()).filter(Boolean);
  return {url: location.href, ready: document.readyState, paso: paso, errores: err.slice(0, 8)};
})()
"""

JS_FORM1 = "(async () => {" + JS_COMUN + r"""
const D = __DATA__;
const falta = [];
const prov = $('#pg_m1_gr2_provincia_titular'), mun = $('#pg_m1_gr2_municipio_titular');
if (D.modo !== undefined && D.modo !== null) {
  const r = $('input[name="pg_m1_gr1_eleccion_de_modo_de_actuacion"][value="' + D.modo + '"]');
  if (r && !r.checked) r.click();
}
const po = findOpt(prov, D.provincia);
if (!po) falta.push('provincia: ' + D.provincia); else if (prov.value !== po.value) { prov.value = po.value; prov.dispatchEvent(new Event('change', {bubbles: true})); }
let mo = null;
for (let i = 0; i < 80; i++) {            // los municipios se cargan al elegir provincia
  mo = findOpt(mun, D.municipio);
  if (mo) break;
  await sleep(250);
}
if (!mo) falta.push('municipio: ' + D.municipio); else { mun.value = mo.value; mun.dispatchEvent(new Event('change', {bubbles: true})); }
const campos = {
  pg_m1_gr2_direccion_del_titular: D.direccion,
  pg_m1_gr2_codigo_postal_titular: D.codigo_postal,
  pg_m1_gr2_telefono_titular: D.telefono,
  pg_m1_gr2_correo_electronico_titular: D.correo
};
for (const k in campos) {
  const el = document.getElementById(k);
  if (!el) { falta.push('campo ' + k); continue; }
  if (campos[k] !== undefined && campos[k] !== null && campos[k] !== '') setVal(el, String(campos[k]));
}
const chk = $('#chk-pg_m1_gr4_notificacioneselectronicas');
if (chk && !chk.checked) chk.click();
await sleep(300);
const ce = $('#pg_m1_gr4_correo_electronico_notificaciones');
if (!ce) falta.push('correo de notificaciones'); else setVal(ce, String(D.correo_notificaciones || D.correo || ''));
const leer = id => { const e = document.getElementById(id); return e ? e.value : null; };
return {
  falta: falta,
  valores: {
    provincia: prov.options[prov.selectedIndex] && prov.options[prov.selectedIndex].textContent.trim(),
    municipio: mun.options[mun.selectedIndex] && mun.options[mun.selectedIndex].textContent.trim(),
    direccion: leer('pg_m1_gr2_direccion_del_titular'), cp: leer('pg_m1_gr2_codigo_postal_titular'),
    telefono: leer('pg_m1_gr2_telefono_titular'), correo: leer('pg_m1_gr2_correo_electronico_titular'),
    correo_notif: leer('pg_m1_gr4_correo_electronico_notificaciones'),
    notif_electronica: !!(chk && chk.checked)
  }
};
})()"""

JS_FORM2 = "(async () => {" + JS_COMUN + r"""
const D = __DATA__;
const falta = [];
const m = {pg_m2_gr1_asunto: D.asunto, pg_m2_gr1_destinatario: D.destinatario,
           pg_m2_gr2_expongo: D.expongo, pg_m2_gr2_solicito: D.solicito};
for (const k in m) {
  const el = document.getElementById(k);
  if (!el) { falta.push(k); continue; }
  setVal(el, String(m[k] || ''));
}
return {falta: falta, longitudes: Object.fromEntries(Object.keys(m).map(k => [k, (document.getElementById(k) || {value: ''}).value.length]))};
})()"""

JS_ADJUNTO = "(async () => {" + JS_COMUN + r"""
const D = __DATA__;
const btn = $('button[data-guid="pg_m2_gr3_documentacion_adjunta"]');
if (!btn) return {ok: false, error: 'no encuentro el boton Nuevo de adjuntos'};
btn.click();
let inp = null, modal = $('#repeater-modal');
for (let i = 0; i < 60; i++) {
  await sleep(250);
  inp = $('input[type="file"]', modal);
  if (inp) break;
}
if (!inp) return {ok: false, error: 'el formulario de adjuntar no aparecio (o no tiene campo de fichero)'};
const bin = atob(D.b64), u8 = new Uint8Array(bin.length);
for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
const dt = new DataTransfer();
dt.items.add(new File([u8], D.nombre, {type: D.mime}));
inp.files = dt.files;
inp.dispatchEvent(new Event('input', {bubbles: true}));
inp.dispatchEvent(new Event('change', {bubbles: true}));
const txt = $$('input[type="text"], textarea', modal).filter(e => !e.disabled);
if (txt.length && D.descripcion) setVal(txt[0], D.descripcion);
await sleep(600);
const botones = $$('button', modal).filter(b => !b.disabled);
let guardar = botones.find(b => /guardar|aceptar|a[nñ]adir|crear|subir|enviar/i.test(b.textContent)) ||
              botones.find(b => b.type === 'submit') || $('.modal-footer .btn-primary', modal);
if (!guardar) return {ok: false, error: 'no encuentro el boton de guardar del adjunto', botones: botones.map(b => b.textContent.trim())};
guardar.click();
for (let i = 0; i < 120; i++) {
  await sleep(250);
  const abierto = modal && (modal.classList.contains('show') || getComputedStyle(modal).display !== 'none');
  if (!abierto) break;
}
const cerrado = !(modal && (modal.classList.contains('show') || getComputedStyle(modal).display !== 'none'));
const visto = document.body.innerText.indexOf(D.nombre) >= 0;
return {ok: cerrado, aparece_en_lista: visto, error: cerrado ? null : 'el cuadro de adjuntar sigue abierto (¿error de validacion?)'};
})()"""

JS_CLICK_SIGN = "(() => { const b = document.querySelector('#SignButton'); if (!b) return false; b.click(); return true; })()"
JS_SIGUIENTE = r"""(() => {
  const f = document.querySelector('#mainForm') || document;
  const b = Array.from(f.querySelectorAll('button[type="submit"]')).find(x => !x.disabled);
  if (!b) return false; b.click(); return true; })()"""
JS_BORRADOR = "(() => { const b = document.querySelector('#saveFormDraftBtn'); if (!b) return false; b.click(); return true; })()"
JS_RESUMEN = r"""(() => { const m = document.querySelector('#mainForm') || document.querySelector('main') || document.body;
  return m.innerText.replace(/\n{3,}/g, '\n\n').slice(0, 3000); })()"""


def inyectar(js, datos):
    return js.replace("__DATA__", json.dumps(datos, ensure_ascii=False))


# ----------------------------------------------------------------------------
# flujo
# ----------------------------------------------------------------------------
def leer_texto(v):
    if isinstance(v, str) and v.startswith("@"):
        ruta = os.path.expanduser(v[1:])
        try:
            with open(ruta, encoding="utf-8") as f:
                return f.read().strip()
        except OSError as e:
            die("No puedo leer %s: %s" % (ruta, e))
    return v


def cargar_datos(ruta):
    try:
        with open(os.path.expanduser(ruta), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        die("No puedo leer %s: %s" % (ruta, e))
    faltan = [k for k in ("provincia", "municipio", "direccion", "codigo_postal", "asunto",
                          "destinatario", "expongo", "solicito") if not str(d.get(k, "")).strip()]
    if faltan:
        die("Faltan datos obligatorios en %s: %s" % (ruta, ", ".join(faltan)))
    if "Queja por ..." in d["asunto"] or d["expongo"].startswith("Primero.- ..."):
        die("El fichero todavia tiene los textos de ejemplo. Editalo (asunto, destinatario, expongo, solicito).")
    if d.get("notificacion", "electronica") != "electronica":
        die("Solo esta soportada la notificacion 'electronica' (la postal no aparece en las capturas).")
    d["expongo"] = leer_texto(d["expongo"])
    d["solicito"] = leer_texto(d["solicito"])
    d["correo_notificaciones"] = d.get("correo_notificaciones") or d.get("correo") or ""
    if not d["correo_notificaciones"]:
        die("Indica 'correo_notificaciones' (es obligatorio para la notificacion electronica).")
    adj = []
    for a in d.get("adjuntos") or []:
        ruta = os.path.expanduser(a["ruta"])
        if not os.path.isfile(ruta):
            die("No existe el adjunto: %s" % ruta)
        if os.path.getsize(ruta) > 15 * 1024 * 1024:
            die("Adjunto demasiado grande (>15 MB): %s" % ruta)
        adj.append(a)
    d["adjuntos"] = adj
    return d


def esperar_paso(ch, validos, timeout=60, adb=None, auto_cert=False, boton_cert=None):
    """Espera a que la pagina este en uno de los pasos 'validos'. Devuelve el estado."""
    t0 = time.time()
    ultimo_tap = 0
    while time.time() - t0 < timeout:
        st = ch.safe_eval(JS_ESTADO)
        if st and st["ready"] == "complete" and st["paso"] in validos:
            return st
        if auto_cert and adb and "cert" in validos and time.time() - ultimo_tap > 2.5:
            ultimo_tap = time.time()
            intentar_certificado(adb, boton_cert)
        time.sleep(0.7)
    return ch.safe_eval(JS_ESTADO)


def intentar_certificado(adb, boton_cert=None):
    nodes = parse_nodes(adb.ui_dump())
    acc = plan_certificado(nodes, boton_cert)
    for x, y, motivo in acc:
        log("   [cert] pulsando %s" % motivo)
        adb.tap(x, y)
        time.sleep(0.8)
    return bool(acc)


def paso_certificado(ch, adb, args):
    log("-> Certificado digital: pulsando 'Seleccionar certificado'")
    ch.safe_eval(JS_CLICK_SIGN)
    adb.traer_chrome()
    auto = not args.cert_manual
    if auto:
        log("   Intentando elegir el certificado automaticamente (adb). Si ves el cuadro, puedes elegirlo tu.")
    else:
        log("   ELIGE EL CERTIFICADO en el cuadro de Chrome (tienes %d s)." % args.espera_cert)
    t0 = time.time()
    ultimo = 0
    while time.time() - t0 < args.espera_cert:
        st = ch.safe_eval(JS_ESTADO)
        if st and st["paso"] != "cert":
            return st
        if auto and time.time() - ultimo > 2.5:
            ultimo = time.time()
            intentar_certificado(adb, args.boton_cert)
        time.sleep(0.8)
    die("No se completo la eleccion del certificado en %d s. Repite o usa --cert-manual." % args.espera_cert)


def asegurar_conexion(args):
    adb = Adb()
    adb.attach()
    adb.forward()
    ch = Chrome()
    try:
        ch.attach()
    except ConnectionError:
        log("Abriendo Chrome...")
        adb.abrir_url(URL_INSTANCIA)
        for _ in range(30):
            time.sleep(1)
            try:
                ch.attach()
                break
            except Exception:
                pass
        else:
            die("No consigo hablar con Chrome por DevTools. Mira el manual (apartado Problemas).")
    return adb, ch


def cmd_presentar(args):
    datos = cargar_datos(args.datos)
    if shutil.which("termux-wake-lock"):
        subprocess.run(["termux-wake-lock"])
    adb, ch = asegurar_conexion(args)
    log("Conectado a Chrome. Pestana: %s" % ch.tab.get("url", "")[:90])

    st = ch.safe_eval(JS_ESTADO)
    if not st or st["paso"] in ("otro", "instancia", "procedimiento") or SEDE.split("//", 1)[1] not in st["url"]:
        log("-> Abriendo la sede (Iniciar tramite)")
        ch.call("Page.navigate", {"url": URL_INICIO})
        st = esperar_paso(ch, ("cert", "form1", "form2", "confirmacion", "instancia"), 60)

    for _ in range(12):                                   # maquina de estados
        paso = st["paso"] if st else "otro"
        log("   estado: %s" % paso)
        if paso == "instancia":
            log("-> Pulsando 'Iniciar tramite' otra vez")
            ch.call("Page.navigate", {"url": URL_INICIO})
            st = esperar_paso(ch, ("cert", "form1", "form2", "confirmacion"), 60)
        elif paso == "cert":
            st = paso_certificado(ch, adb, args)
            st = esperar_paso(ch, ("form1", "form2", "confirmacion", "instancia"), 60)
        elif paso == "form1":
            log("-> Paso 1: datos del solicitante y del titular")
            r = ch.safe_eval(inyectar(JS_FORM1, datos), timeout=90)
            if not r:
                die("No pude rellenar el paso 1.")
            if r["falta"]:
                die("Paso 1: no encuentro: %s" % "; ".join(r["falta"]))
            log("   %s" % json.dumps(r["valores"], ensure_ascii=False))
            if args.borrador:
                ch.safe_eval(JS_BORRADOR)
                log("Borrador guardado en el paso 1.")
                return 0
            if not ch.safe_eval(JS_SIGUIENTE):
                die("No encuentro el boton Siguiente del paso 1.")
            st = esperar_paso(ch, ("form2", "confirmacion"), 40)
            if st["paso"] == "form1":
                die("El paso 1 no avanza. Errores de la web: %s" % ("; ".join(st["errores"]) or "ninguno visible"))
        elif paso == "form2":
            log("-> Paso 2: datos de la solicitud")
            r = ch.safe_eval(inyectar(JS_FORM2, datos), timeout=60)
            if not r or r["falta"]:
                die("Paso 2: no encuentro campos: %s" % (r or {}).get("falta"))
            log("   textos puestos: %s" % json.dumps(r["longitudes"]))
            pendientes = []
            for a in datos["adjuntos"]:
                log("-> Adjuntando %s" % a["ruta"])
                ruta = os.path.expanduser(a["ruta"])
                with open(ruta, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                mime = mimetypes.guess_type(ruta)[0] or "application/octet-stream"
                res = ch.safe_eval(inyectar(JS_ADJUNTO, {
                    "b64": b64, "nombre": os.path.basename(ruta), "mime": mime,
                    "descripcion": a.get("descripcion", os.path.basename(ruta))}), timeout=150)
                if not res or not res.get("ok"):
                    log("   NO se pudo adjuntar automaticamente: %s" % (res or {}).get("error"))
                    pendientes.append(a["ruta"])
                else:
                    log("   adjuntado (aparece en la lista: %s)" % res.get("aparece_en_lista"))
            if pendientes:
                adb.traer_chrome()
                print("\nAdjunta a mano en Chrome: %s" % ", ".join(pendientes))
                input("Cuando termines pulsa Intro aqui para continuar... ")
            if args.borrador:
                ch.safe_eval(JS_BORRADOR)
                log("Borrador guardado en el paso 2.")
                return 0
            if not ch.safe_eval(JS_SIGUIENTE):
                die("No encuentro el boton Siguiente del paso 2.")
            st = esperar_paso(ch, ("confirmacion",), 40)
            if st["paso"] == "form2":
                die("El paso 2 no avanza. Errores de la web: %s" % ("; ".join(st["errores"]) or "ninguno visible"))
        elif paso == "confirmacion":
            adb.traer_chrome()
            print("\n=== LLEGASTE A LA CONFIRMACION ===")
            print("Revisa los datos en Chrome y firma/presenta tu mismo. Este programa NO envia el escrito.\n")
            print((ch.safe_eval(JS_RESUMEN) or "")[:1500])
            return 0
        else:
            die("Pagina inesperada (%s): %s" % (paso, (st or {}).get("url")))
    die("Demasiados reintentos en el flujo.")


def cmd_plantilla(args):
    ruta = os.path.expanduser(args.fichero)
    if os.path.exists(ruta) and not args.forzar:
        die("%s ya existe (usa --forzar para sobrescribir)." % ruta)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(PLANTILLA, f, ensure_ascii=False, indent=2)
    print("Creado %s. Edita asunto, destinatario, expongo, solicito y adjuntos." % ruta)


def cmd_adb_emparejar(args):
    adb = Adb()
    print("En el tablet: Ajustes > Opciones de desarrollador > Depuracion inalambrica >")
    print("'Vincular dispositivo con codigo de vinculacion'. Escribe lo que muestra:")
    hp = input("IP:puerto de vinculacion (ej. 127.0.0.1:37215): ").strip()
    code = input("Codigo de 6 digitos: ").strip()
    rc, out = adb.run("pair", hp, code, serial=False, timeout=60)
    print(out.strip())
    return 0 if rc == 0 and "Successfully" in out else 1


def cmd_adb_conectar(args):
    adb = Adb()
    rc, out = adb.run("connect", "127.0.0.1:%s" % args.puerto, serial=False, timeout=30)
    print(out.strip())
    if "connected" not in out:
        return 1
    adb.attach()
    adb.forward()
    print("Listo. Dispositivo: %s. Acceso a Chrome en 127.0.0.1:%d" % (adb.serial, CDP_PORT))
    return 0


def cmd_estado(args):
    adb = Adb()
    devs = adb.devices()
    print("adb: %s" % (", ".join("%s (%s)" % d for d in devs) or "sin dispositivos"))
    if not any(s == "device" for _, s in devs):
        return 1
    adb.attach()
    adb.forward()
    ch = Chrome()
    try:
        tabs = ch.tabs()
    except ConnectionError as e:
        print("Chrome: %s" % e)
        return 1
    print("Chrome: %d pestana(s)" % len(tabs))
    for t in tabs[:6]:
        print("   - %s" % t.get("url", "")[:100])
    ch.attach()
    print("Pagina de la sede: %s" % json.dumps(ch.safe_eval(JS_ESTADO), ensure_ascii=False))
    return 0


def cmd_ui(args):
    adb = Adb()
    adb.attach()
    nodes = parse_nodes(adb.ui_dump())
    if not nodes:
        print("No pude leer la pantalla (uiautomator).")
        return 1
    for n in nodes:
        if n["text"] and (n["clickable"] or n["checkable"]):
            print("%-10s %-40s pkg=%s checked=%s" % ("boton" if n["clickable"] else "casilla", n["text"][:40], n["pkg"], n["checked"]))
    print("\nPlan para certificado: %s" % (plan_certificado(nodes, args.boton_cert) or "no hay cuadro de certificado"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="queja.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("plantilla"); s.add_argument("fichero", nargs="?", default="queja.json"); s.add_argument("--forzar", action="store_true")
    sub.add_parser("adb-emparejar")
    s = sub.add_parser("adb-conectar"); s.add_argument("puerto")
    sub.add_parser("estado")
    s = sub.add_parser("ui"); s.add_argument("--boton-cert")
    s = sub.add_parser("presentar"); s.add_argument("datos")
    s.add_argument("--cert-manual", action="store_true", help="no pulsar el certificado por adb: lo eliges tu")
    s.add_argument("--boton-cert", help="texto del boton del cuadro de certificado si no es el habitual")
    s.add_argument("--espera-cert", type=int, default=120, help="segundos maximos para elegir certificado")
    s.add_argument("--borrador", action="store_true", help="guardar borrador en lugar de avanzar")
    a = p.parse_args(argv)
    f = {"plantilla": cmd_plantilla, "adb-emparejar": cmd_adb_emparejar, "adb-conectar": cmd_adb_conectar,
         "estado": cmd_estado, "ui": cmd_ui, "presentar": cmd_presentar}[a.cmd]
    try:
        return f(a) or 0
    except KeyboardInterrupt:
        print("\nInterrumpido.")
        return 130
    except (ConnectionError, TimeoutError, OSError) as e:
        die("Conexion con Chrome perdida: %s" % e)


if __name__ == "__main__":
    sys.exit(main())
