#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagtermux.py  -  Vigilante de Termux y lanzador de comandos largos

Sirve para dos cosas:

  1) Averiguar POR QUE se cuelga o se cierra Termux/tmux. Un vigilante muy
     ligero anota cada pocos segundos memoria, procesos y consumo; despues de
     un cierre, el comando "informe" te dice que pasaba justo antes.

  2) Lanzar comandos largos sin cargar la pantalla de Termux/tmux. Se ejecutan
     separados del terminal, con prioridad baja, con wake-lock y con su log.

Uso rapido:
  python diagtermux.py vigilar              arranca el vigilante (segundo plano)
  python diagtermux.py informe              analiza los cierres anormales
  python diagtermux.py estado               foto del momento actual
  python diagtermux.py parar                para el vigilante
  python diagtermux.py lanzar -- COMANDO    lanza un comando largo desacoplado
  python diagtermux.py trabajos             lista los comandos lanzados
  python diagtermux.py ver ID               ultimas lineas del log de un trabajo
  python diagtermux.py matar ID             detiene un trabajo

Solo usa la libreria estandar de Python. Todo se guarda en ~/diagtermux/
"""
import argparse
import datetime
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import time

VERSION = "1.0"
HOME = os.path.expanduser("~")
DIR = os.environ.get("DIAGTERMUX_DIR") or os.path.join(HOME, "diagtermux")
LOG = os.path.join(DIR, "diagtermux.log")
PIDF = os.path.join(DIR, "vigilante.pid")
TRABAJOS = os.path.join(DIR, "trabajos")
MAX_LOG = 4 * 1024 * 1024                      # al pasar de 4 MB se rota (quedan .1 y .2)


def _paquete_termux():
    """Nombre del paquete de la app, sacado de $PREFIX (/data/data/<paquete>/files/usr)."""
    m = re.match(r"/data/(?:data|user/\d+)/([^/]+)/", os.environ.get("PREFIX", ""))
    return m.group(1) if m else None


APP = os.environ.get("DIAGTERMUX_APP") or _paquete_termux() or "com.termux"   # proceso principal
HZ = os.sysconf("SC_CLK_TCK")
PAGINA = os.sysconf("SC_PAGE_SIZE")
TS = "%Y-%m-%d %H:%M:%S"
INTERPRETES = ("python", "bash", "sh", "node", "perl", "ruby", "java")


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def ahora():
    return datetime.datetime.now().strftime(TS)


def leer(ruta):
    try:
        with open(ruta, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def leer_cola(ruta, nbytes=65536):
    """Ultimos nbytes de un archivo (para no leer registros enormes)."""
    try:
        with open(ruta, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def anadir_linea(ruta, texto):
    with open(ruta, "a", encoding="utf-8", errors="replace") as f:
        f.write(texto + "\n")


def a_fecha(txt):
    try:
        return datetime.datetime.strptime(txt, TS)
    except (ValueError, TypeError):
        return None


def ancho():
    return max(50, min(shutil.get_terminal_size((80, 24)).columns, 100) - 2)


def parrafo(texto, sangria=4, continuacion=None):
    """Texto largo ajustado al ancho de la pantalla de Termux."""
    if continuacion is None:
        continuacion = sangria
    return textwrap.fill(texto, width=ancho(), initial_indent=" " * sangria,
                         subsequent_indent=" " * continuacion)


def duracion(segundos):
    s = int(max(0, segundos))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%d d %d h" % (d, h)
    if h:
        return "%d h %d min" % (h, m)
    if m:
        return "%d min %d s" % (m, s)
    return "%d s" % s


def prop(nombre):
    """Lee una propiedad de Android (getprop). Vacio si no existe."""
    try:
        r = subprocess.run([shutil.which("getprop") or "/system/bin/getprop", nombre],
                           capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def info_sistema():
    d = {}
    for clave, nombre in (("sdk", "ro.build.version.sdk"),
                          ("android", "ro.build.version.release"),
                          ("modelo", "ro.product.model"),
                          ("emui", "ro.build.version.emui"),
                          ("harmony", "hw_sc.build.platform.version")):
        v = prop(nombre)
        if v:
            d[clave] = v
    if os.environ.get("TERMUX_VERSION"):
        d["termux"] = os.environ["TERMUX_VERSION"]
    return d


# --------------------------------------------------------------------------
# Lectura de /proc
# --------------------------------------------------------------------------
def meminfo():
    d = {}
    for linea in (leer("/proc/meminfo") or "").splitlines():
        m = re.match(r"(\w+):\s+(\d+)", linea)
        if m:
            d[m.group(1)] = int(m.group(2))
    return d


def argv_de(pid):
    t = leer("/proc/%d/cmdline" % pid)
    return [a for a in t.split("\0") if a] if t else []


def etiqueta(comm, argv):
    """Nombre corto y legible: para python/bash/etc. anade el script."""
    base = comm
    if argv:
        a0 = os.path.basename(argv[0])
        if a0.startswith(INTERPRETES):
            base = a0
            for a in argv[1:]:
                if a == "-c":
                    base = a0 + ":-c"
                    break
                if not a.startswith("-"):
                    base = a0 + ":" + os.path.basename(a)
                    break
    return re.sub(r"[^\w.:+\-]", "_", base)[:28]


def procesos():
    """Procesos del mismo usuario (en Android, los de Termux)."""
    uid = os.getuid()
    res = {}
    try:
        nombres = os.listdir("/proc")
    except OSError:
        return res
    for n in nombres:
        if not n.isdigit():
            continue
        try:
            if os.stat("/proc/" + n).st_uid != uid:
                continue
        except OSError:
            continue
        s = leer("/proc/" + n + "/stat")
        if not s:
            continue
        try:
            i, j = s.index("("), s.rindex(")")
            f = s[j + 2:].split()
            pid = int(n)
            res[pid] = {
                "comm": s[i + 1:j],
                "estado": f[0],
                "ppid": int(f[1]),
                "ticks": int(f[11]) + int(f[12]),
                "hilos": int(f[17]),
                "rss_kb": int(f[21]) * PAGINA // 1024,
                "argv": argv_de(pid),
            }
        except (ValueError, IndexError):
            continue
    return res


def hijos_de_la_app(procs):
    """Cuantos procesos cuelgan del proceso principal de la app. Es lo que
    cuenta el limitador de 'procesos fantasma' de Android 12+."""
    raiz = [p for p, d in procs.items() if d["argv"] and d["argv"][0] == APP]
    if not raiz:
        return None
    hijos = {}
    for p, d in procs.items():
        hijos.setdefault(d["ppid"], []).append(p)
    cuenta, pila = 0, [raiz[0]]
    while pila:
        for h in hijos.get(pila.pop(), []):
            cuenta += 1
            pila.append(h)
    return cuenta


def estado_proc(pid):
    """(vivo, starttime). Un zombi cuenta como muerto."""
    s = leer("/proc/%d/stat" % pid)
    if not s:
        return False, None
    try:
        f = s[s.rindex(")") + 2:].split()
        return f[0] != "Z", int(f[19])
    except (ValueError, IndexError):
        return False, None


def psi():
    m = re.search(r"some avg10=([\d.]+)", leer("/proc/pressure/memory") or "")
    return float(m.group(1)) if m else None


def carga():
    t = leer("/proc/loadavg")
    try:
        return float(t.split()[0])
    except (AttributeError, ValueError, IndexError):
        return None


def bateria():
    base = "/sys/class/power_supply/battery/"
    try:
        cap = int(leer(base + "capacity"))
    except (TypeError, ValueError):
        cap = None
    try:
        temp = int(leer(base + "temp")) / 10.0
    except (TypeError, ValueError):
        temp = None
    return cap, temp


def paneles_tmux():
    if not shutil.which("tmux"):
        return None
    try:
        r = subprocess.run(["tmux", "list-panes", "-a", "-F", "x"],
                           capture_output=True, text=True, timeout=3)
        return len(r.stdout.split()) if r.returncode == 0 else 0
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------
# Muestreo
# --------------------------------------------------------------------------
class Muestreador:
    def __init__(self):
        self.prev = {}
        self.t_prev = None
        self.app_vista = False      # ya hemos visto alguna vez el proceso de la app

    def tomar(self):
        t = time.monotonic()
        mem = meminfo()
        procs = procesos()
        yo = os.getpid()
        cpu = {}
        if self.t_prev:
            dt = max(t - self.t_prev, 0.001)
            for pid, d in procs.items():
                if pid != yo and pid in self.prev:
                    cpu[pid] = (d["ticks"] - self.prev[pid]) * 100.0 / (HZ * dt)
        self.prev = {pid: d["ticks"] for pid, d in procs.items()}
        self.t_prev = t

        def nombre(pid):
            return etiqueta(procs[pid]["comm"], procs[pid]["argv"])

        otros = [p for p in procs if p != yo]
        por_mem = sorted(otros, key=lambda p: -procs[p]["rss_kb"])[:3]
        por_cpu = sorted(cpu, key=lambda p: -cpu[p])[:3]
        cap, temp = bateria()
        zombis = sum(1 for d in procs.values() if d["estado"] == "Z")
        hijos = hijos_de_la_app(procs)
        if hijos is not None:
            self.app_vista = True
        srv = [p for p, d in procs.items() if d["comm"].startswith("tmux: server")]
        return {
            "mem_disp": mem.get("MemAvailable", mem.get("MemFree", 0)) // 1024,
            "mem_tot": mem.get("MemTotal", 0) // 1024,
            "swap_libre": mem.get("SwapFree", 0) // 1024,
            "swap_tot": mem.get("SwapTotal", 0) // 1024,
            "procs": len(procs),
            "hijos": hijos,
            # 1 = proceso de la app presente, 0 = ha desaparecido (solo si antes se vio)
            "app": (0 if hijos is None else 1) if self.app_vista else None,
            "tmuxsrv": srv[0] if srv else None,
            "rss": sum(d["rss_kb"] for d in procs.values()) // 1024,
            "hilos": sum(d["hilos"] for d in procs.values()),
            "tmux": paneles_tmux(),
            "psi": psi(),
            "carga": carga(),
            "bat": cap,
            "temp": temp,
            "zombis": zombis or None,
            "top_mem": [(nombre(p), procs[p]["rss_kb"] // 1024) for p in por_mem],
            "top_cpu": [(nombre(p), int(cpu[p])) for p in por_cpu if cpu[p] >= 1],
        }


def formatear(m):
    p = [ahora(),
         "mem=%d/%d" % (m["mem_disp"], m["mem_tot"]),
         "swap=%d/%d" % (m["swap_libre"], m["swap_tot"]),
         "procs=%d" % m["procs"]]
    if m["hijos"] is not None:
        p.append("hijos=%d" % m["hijos"])
    p += ["rss=%d" % m["rss"], "hilos=%d" % m["hilos"]]
    for k in ("app", "tmux", "tmuxsrv", "psi", "carga", "bat", "temp", "zombis"):
        if m.get(k) is not None:
            p.append("%s=%s" % (k, m[k]))
    if m["top_mem"]:
        p.append("topmem=" + ",".join("%s:%d" % x for x in m["top_mem"]))
    if m["top_cpu"]:
        p.append("topcpu=" + ",".join("%s:%d" % x for x in m["top_cpu"]))
    return " ".join(p)


# --------------------------------------------------------------------------
# Vigilante
# --------------------------------------------------------------------------
def vigilante_pid():
    """PID del vigilante si esta vivo (comprobando que de verdad es el nuestro)."""
    try:
        pid = int((leer(PIDF) or "").strip())
    except ValueError:
        return None
    vivo, _ = estado_proc(pid)
    if vivo and "diagtermux" in " ".join(argv_de(pid)):
        return pid
    return None


def rotar():
    try:
        if os.path.getsize(LOG) > MAX_LOG:
            if os.path.exists(LOG + ".1"):
                os.replace(LOG + ".1", LOG + ".2")
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass


def tmux_previo():
    """Al arrancar: el servidor de tmux que habia en la ultima muestra del
    registro anterior, ¿sigue vivo? Distingue 'solo cayo la ventana' de 'el
    sistema mato todos los procesos de Termux'."""
    for ruta in (LOG, LOG + ".1"):
        t = leer_cola(ruta)
        for ln in reversed((t or "").splitlines()[-300:]):
            s = parsear_muestra(ln)
            if s:
                if not s.get("tmuxsrv"):
                    return None
                vivo, _ = estado_proc(s["tmuxsrv"])      # un zombi cuenta como muerto
                c = leer("/proc/%d/comm" % s["tmuxsrv"]) or ""
                return "vivo" if vivo and c.startswith("tmux: server") else "muerto"
    return None


def bucle(intervalo):
    os.makedirs(DIR, exist_ok=True)
    # Cerrojo exclusivo: aunque dos paneles lo arranquen a la vez, solo uno sigue.
    # El sistema lo libera solo si el proceso muere (incluso con SIGKILL).
    cerrojo = open(os.path.join(DIR, "vigilante.lock"), "w")
    try:
        fcntl.flock(cerrojo, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 1
    previo = tmux_previo()
    with open(PIDF, "w") as f:
        f.write(str(os.getpid()))
    cab = ["# INICIO", ahora(), "pid=%d" % os.getpid(), "intervalo=%d" % intervalo]
    if previo:
        cab.append("tmux_previo=" + previo)
    cab += ["%s=%s" % (k, v.replace(" ", "_")) for k, v in info_sistema().items()]
    anadir_linea(LOG, " ".join(cab))

    parada = {"senal": None}

    def manejador(signum, _frame):
        parada["senal"] = signal.Signals(signum).name

    signal.signal(signal.SIGTERM, manejador)
    signal.signal(signal.SIGINT, manejador)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    mu = Muestreador()
    n = 0
    while not parada["senal"]:
        t0 = time.monotonic()
        try:
            anadir_linea(LOG, formatear(mu.tomar()))
        except OSError:
            pass
        n += 1
        if n % 20 == 0:
            rotar()
        while time.monotonic() - t0 < intervalo and not parada["senal"]:
            time.sleep(0.25)
    anadir_linea(LOG, "# FIN %s limpio senal=%s" % (ahora(), parada["senal"]))
    try:
        os.remove(PIDF)
    except OSError:
        pass
    return 0


def cmd_vigilar(args):
    os.makedirs(DIR, exist_ok=True)
    pid = vigilante_pid()
    if pid and not args.primer_plano:
        print("El vigilante ya está en marcha (PID %d)." % pid)
        return 0
    if args.primer_plano:
        if pid and pid != os.getpid():
            print("Ya hay otro vigilante en marcha (PID %d)." % pid)
            return 1
        return bucle(args.intervalo)
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "vigilar",
         "--primer-plano", "--intervalo", str(args.intervalo)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(0.8)
    print("Vigilante arrancado (PID %d), una muestra cada %d s." % (p.pid, args.intervalo))
    print("Registro: " + LOG)
    return 0


def cmd_parar(_args):
    pid = vigilante_pid()
    if not pid:
        print("El vigilante no está en marcha.")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not estado_proc(pid)[0]:
            break
        time.sleep(0.25)
    print("Vigilante parado.")
    return 0


# --------------------------------------------------------------------------
# Informe de cierres anormales
# --------------------------------------------------------------------------
def parsear_muestra(linea):
    if len(linea) < 21 or linea[4] != "-" or linea[:1] == "#":
        return None
    d = {"ts": linea[:19]}
    d.update(re.findall(r"(\w+)=(\S+)", linea[20:]))

    def par(clave):
        try:
            a, b = d[clave].split("/")
            return int(a), int(b)
        except (KeyError, ValueError):
            return None, None

    d["mem_disp"], d["mem_tot"] = par("mem")
    d["swap_libre"], d["swap_tot"] = par("swap")
    for k in ("procs", "hijos", "rss", "hilos", "tmux", "tmuxsrv", "app", "bat"):
        try:
            d[k] = int(d[k])
        except (KeyError, ValueError):
            d[k] = None
    for k in ("psi", "carga", "temp"):
        try:
            d[k] = float(d[k])
        except (KeyError, ValueError):
            d[k] = None
    return d


def cargar_ejecuciones():
    """Divide el registro en ejecuciones del vigilante."""
    lineas = []
    for ruta in (LOG + ".2", LOG + ".1", LOG):
        t = leer(ruta)
        if t:
            lineas += t.splitlines()
    ejecs, actual = [], None
    for ln in lineas:
        if ln.startswith("# INICIO"):
            m = re.search(r"intervalo=(\d+)", ln)
            actual = {"inicio": ln[9:28], "fin": None, "muestras": [],
                      "intervalo": int(m.group(1)) if m else 5,
                      "info": dict(re.findall(r"(\w+)=(\S+)", ln[29:]))}
            ejecs.append(actual)
        elif ln.startswith("# FIN"):
            if actual is not None:
                actual["fin"] = ln[6:25]
        else:
            s = parsear_muestra(ln)
            if s:
                if actual is None:      # el INICIO se perdio al rotar el registro
                    actual = {"inicio": None, "fin": None, "muestras": [],
                              "intervalo": 5, "info": {}}
                    ejecs.append(actual)
                actual["muestras"].append(s)
    return ejecs


def etiqueta_top(txt):
    """'python:fb.py:85,bash:3' -> ('python:fb.py', 85)"""
    try:
        nombre, val = txt.split(",")[0].rsplit(":", 1)
        return nombre, int(val)
    except (ValueError, AttributeError):
        return None, 0


def indicios(ult, intervalo):
    """(pistas, categorias) a partir de las ultimas muestras de una ejecucion."""
    pistas, cats = [], []
    if not ult:
        return ["No hay muestras en esta ejecución (el cierre ocurrió nada más arrancar)."], ["ninguno"]
    memd = [s["mem_disp"] for s in ult if s["mem_disp"] is not None]
    memt = next((s["mem_tot"] for s in reversed(ult) if s["mem_tot"]), 0)
    swl = [s["swap_libre"] for s in ult if s["swap_libre"] is not None]
    swt = next((s["swap_tot"] for s in reversed(ult) if s["swap_tot"]), 0)
    psis = [s["psi"] for s in ult if s["psi"] is not None]
    procs = [s["procs"] for s in ult if s["procs"] is not None]
    memoria = False
    if memd and memt and min(memd) < max(300, 0.07 * memt):
        memoria = True
        pistas.append("MEMORIA: la memoria disponible llegó a %d MB de %d MB."
                      % (min(memd), memt))
    if swt >= 256 and swl and min(swl) < 0.10 * swt:
        memoria = True
        pistas.append("MEMORIA: el swap estaba casi agotado (%d MB libres de %d MB)."
                      % (min(swl), swt))
    if psis and max(psis) >= 15:
        memoria = True
        pistas.append("MEMORIA: presión de memoria alta (psi %.1f)." % max(psis))
    if memoria:
        cats.append("memoria")
        pistas.append("  -> Compatible con cierre por falta de RAM (en adb sale como "
                      "LOW_MEMORY / lmkd). Cierra Firefox/Chrome mientras trabajas en "
                      "Termux y reduce paneles o procesos pesados.")
    if procs and max(procs) >= 24:
        cats.append("procesos")
        pistas.append("PROCESOS: llegó a %d procesos de Termux a la vez." % max(procs))
        pistas.append("  -> Android 12+ mata procesos hijos cuando pasan de 32 entre TODAS "
                      "las apps (límite compartido). Con tantos procesos de Termux estás "
                      "cerca de ese límite: reduce paneles y comandos con tuberías "
                      "simultáneos (ver el manual).")
    cpus = [etiqueta_top(s.get("topcpu")) for s in ult[-3:]]
    if len(cpus) == 3 and all(n and v >= 85 for n, v in cpus) and len({n for n, _ in cpus}) == 1:
        cats.append("cpu")
        pistas.append("CPU: «%s» estaba al %d %% de un núcleo de forma sostenida." % cpus[-1])
        pistas.append("  -> Android 12 también mata procesos por consumo excesivo de CPU. "
                      "Lánzalo con prioridad baja (comando «lanzar»).")
    huecos = []
    for a, b in zip(ult, ult[1:]):
        fa, fb = a_fecha(a["ts"]), a_fecha(b["ts"])
        if fa and fb and (fb - fa).total_seconds() > max(20, 4 * intervalo):
            huecos.append((a["ts"][11:], int((fb - fa).total_seconds())))
    if huecos:
        cats.append("huecos")
        pistas.append("HUECOS: el sistema dejó al vigilante sin ejecutarse (%s). Puede "
                      "ser la tablet bloqueada/congelada o en suspensión."
                      % ", ".join("%s: %d s" % h for h in huecos[:3]))
    ausentes = sum(1 for s in ult if s.get("app") == 0)
    if ausentes:
        cats.append("app")
        pistas.append("APP: el proceso de la app (%s) ya había desaparecido en las "
                      "últimas %d muestras antes de que muriera el vigilante." % (APP, ausentes))
        pistas.append("  -> Primero cayó la app y poco después el resto de procesos de "
                      "Termux: típico de una detención forzada por el sistema (gestor "
                      "de energía) o de un cierre por memoria.")
    if not pistas:
        cats.append("ninguno")
        pistas.append("Nada anormal en lo que mide el vigilante justo antes del cierre "
                      "(memoria, procesos y CPU normales).")
        pistas.append("  -> Lo más probable es que el sistema cerrara Termux por política "
                      "de energía (gestor de batería de Huawei). Revisa «Inicio de "
                      "aplicaciones» y el bloqueo en tareas recientes (ver manual).")
    return pistas, cats


def tabla_muestras(muestras):
    cab = "%-8s %7s %7s %6s %6s %6s %5s  %s" % (
        "hora", "libre", "swapL", "procs", "hijos", "RSS", "psi", "CPU máxima")
    filas = [cab]
    for s in muestras:
        n, v = etiqueta_top(s.get("topcpu"))
        filas.append("%-8s %7s %7s %6s %6s %6s %5s  %s" % (
            s["ts"][11:],
            s["mem_disp"] if s["mem_disp"] is not None else "-",
            s["swap_libre"] if s["swap_libre"] is not None else "-",
            s["procs"] if s["procs"] is not None else "-",
            s["hijos"] if s["hijos"] is not None else "-",
            s["rss"] if s["rss"] is not None else "-",
            ("%.1f" % s["psi"]) if s["psi"] is not None else "-",
            ("%s %d%%" % (n, v)) if n else "-"))
    return "\n".join("    " + f for f in filas)


def cmd_informe(args):
    ejecs = cargar_ejecuciones()
    if not ejecs:
        print("Todavía no hay registro. Arranca el vigilante con:  python diagtermux.py vigilar")
        return 0
    viva = vigilante_pid()
    anomalos = []
    for i, e in enumerate(ejecs):
        if e["fin"] is not None:
            continue
        ultima_ejec = (i == len(ejecs) - 1)
        if ultima_ejec and viva:
            continue
        reinicio = None if ultima_ejec else ejecs[i + 1]["inicio"]
        anomalos.append((e, reinicio, None if ultima_ejec else ejecs[i + 1]))

    primera = next((e["inicio"] or e["muestras"][0]["ts"] for e in ejecs
                    if e["inicio"] or e["muestras"]), "?")
    print("Informe de cierres  (registro desde %s)" % primera)
    print("Ejecuciones del vigilante: %d   cierres anormales: %d   %s" % (
        len(ejecs), len(anomalos),
        "(vigilante en marcha ahora)" if viva else "(vigilante parado)"))
    if not anomalos:
        print()
        print(parrafo("No hay cierres anormales registrados. Cuando Termux se cuelgue o "
                      "se cierre, vuelve a abrirlo, arranca el vigilante y ejecuta este "
                      "informe.", 0))
        return 0

    print()
    print(parrafo("Cierre anormal = el vigilante dejó de escribir sin despedirse: lo "
                  "mató el sistema, se apagó la tablet o se quedó sin batería (también "
                  "cuenta si fuerzas la detención de Termux a mano).", 0))

    # Resumen de todos los cierres del registro (un cierre puede tener varios indicios)
    cuenta = {"memoria": 0, "procesos": 0, "cpu": 0, "huecos": 0, "app": 0, "ninguno": 0}
    tmux_vivo = tmux_muerto = 0
    for e, _r, sig in anomalos:
        for c in indicios(e["muestras"][-12:], e["intervalo"])[1]:
            cuenta[c] += 1
        previo = sig["info"].get("tmux_previo") if sig else None
        tmux_vivo += previo == "vivo"
        tmux_muerto += previo == "muerto"
    print()
    print(parrafo("Resumen de %d cierres. Con indicios de: memoria %d, exceso de "
                  "procesos %d, CPU %d, congelación/huecos %d, app caída antes %d; "
                  "sin indicios %d. El servidor de tmux sobrevivió en %d y desapareció "
                  "en %d."
                  % (len(anomalos), cuenta["memoria"], cuenta["procesos"], cuenta["cpu"],
                     cuenta["huecos"], cuenta["app"], cuenta["ninguno"], tmux_vivo,
                     tmux_muerto), 0))

    for e, reinicio, siguiente in anomalos[-args.ultimos:]:
        ms = e["muestras"]
        ultima = ms[-1]["ts"] if ms else e["inicio"]
        print("\n" + "=" * min(70, ancho()))
        print("Cierre anormal  %s" % ultima)
        if reinicio and a_fecha(reinicio) and a_fecha(ultima):
            print("  Sin vigilante durante %s (hasta %s)" % (
                duracion((a_fecha(reinicio) - a_fecha(ultima)).total_seconds()), reinicio))
        sis = {k: v for k, v in e["info"].items()
               if k in ("sdk", "android", "modelo", "emui", "harmony", "termux")}
        if sis:
            print("  Sistema: " + ", ".join("%s=%s" % kv for kv in sis.items()))
        if siguiente and siguiente["info"].get("tmux_previo"):
            if siguiente["info"]["tmux_previo"] == "vivo":
                print(parrafo("Al reabrir, el servidor de tmux anterior SEGUÍA VIVO: no se "
                              "mataron todos los procesos de Termux. Cayó la ventana/app "
                              "o solo algunos procesos sueltos (p. ej. el sistema eligió "
                              "procesos concretos para liberar memoria). Prueba «tmux "
                              "attach»: tus sesiones pueden estar intactas.", 2))
            else:
                print(parrafo("Al reabrir, el servidor de tmux anterior YA NO EXISTÍA: "
                              "desaparecieron también los procesos que no dependen de la "
                              "ventana, tmux incluido. Es lo típico de «forzar "
                              "detención» de los gestores de energía de Huawei, o de un "
                              "cierre por memoria que alcanzó a todo Termux.", 2))
        print("  Últimas muestras antes del cierre:")
        ult = ms[-8:]
        print(tabla_muestras(ult) if ult else "    (ninguna)")
        print("  Qué indica:")
        for p in indicios(ms[-12:], e["intervalo"])[0]:
            if p.startswith("  ->"):
                print(parrafo(p.strip(), 6, 9))
            else:
                print(parrafo(p, 4, 6))

    caidas = episodios_sin_app(ejecs)
    if caidas:
        print("\n" + "=" * min(70, ancho()))
        print("Veces que desapareció la app (com.termux) con el vigilante aún vivo:")
        for desde, hasta in caidas[-5:]:
            print("    desde %s  hasta %s" % (desde, hasta or "(sigue ausente)"))
        print(parrafo("Si el vigilante seguía vivo, el sistema mató solo el proceso de "
                      "la app y los procesos desacoplados (tmux, trabajos «lanzar») "
                      "sobrevivieron.", 2))
    print()
    print(parrafo("Leyenda: libre = MB de RAM disponible en todo el sistema; swapL = MB "
                  "de swap libres; procs = procesos de Termux; hijos = procesos que "
                  "cuelgan de la app; RSS = MB que ocupan los procesos de Termux; psi = "
                  "presión de memoria (si el sistema deja leerla). Todo son indicios, "
                  "no pruebas.", 0))
    return 0


def episodios_sin_app(ejecs):
    """Tramos en los que el proceso de la app desaparecio con el vigilante vivo."""
    res = []
    for e in ejecs:
        desde = None
        for s in e["muestras"]:
            if s.get("app") == 0 and desde is None:
                desde = s["ts"]
            elif s.get("app") == 1 and desde is not None:
                res.append((desde, s["ts"]))
                desde = None
        if desde is not None:
            res.append((desde, None))
    return res


# --------------------------------------------------------------------------
# Estado actual
# --------------------------------------------------------------------------
def cmd_estado(_args):
    mu = Muestreador()
    mu.tomar()
    time.sleep(1.0)
    m = mu.tomar()
    print("Estado de Termux - " + ahora())
    pct = 100.0 * m["mem_disp"] / m["mem_tot"] if m["mem_tot"] else 0
    print("  Memoria libre    : %d MB de %d MB (%.0f %%)" % (m["mem_disp"], m["mem_tot"], pct))
    print("  Swap libre       : %d MB de %d MB" % (m["swap_libre"], m["swap_tot"]))
    print("  Procesos Termux  : %d  (hilos %d, RSS %d MB)" % (m["procs"], m["hilos"], m["rss"]))
    if m["hijos"] is not None:
        print("  Hijos de la app  : %d   (límite Android 12+: 32 entre todas las apps)"
              % m["hijos"])
    if m["tmux"] is not None:
        print("  Paneles de tmux  : %d" % m["tmux"])
    if m["top_mem"]:
        print("  Más memoria      : " + ", ".join("%s %d MB" % x for x in m["top_mem"]))
    if m["top_cpu"]:
        print("  Más CPU          : " + ", ".join("%s %d%%" % x for x in m["top_cpu"]))
    if m["bat"] is not None:
        print("  Batería          : %d %%%s" % (m["bat"],
              ("  %.1f °C" % m["temp"]) if m["temp"] is not None else ""))
    inf = info_sistema()
    print("  Sistema          : " + (", ".join("%s=%s" % kv for kv in inf.items())
                                     if inf else "(sin datos de Android: getprop no disponible)"))
    pid = vigilante_pid()
    print("  Vigilante        : " + ("en marcha (PID %d)" % pid if pid else
                                     "PARADO  -> python diagtermux.py vigilar"))
    en_marcha = sum(1 for j in cargar_trabajos() if estado_trabajo(j)[0] == "en marcha")
    print("  Trabajos lanzados: %d en marcha" % en_marcha)
    return 0


# --------------------------------------------------------------------------
# Lanzador de comandos largos
# --------------------------------------------------------------------------
ENVOLTURA = ('"$@"; rc=$?; printf "\\n[larga] FIN rc=%s %s\\n" "$rc" '
             '"$(date "+%F %T")"; exit "$rc"')


def cargar_trabajos():
    res = []
    try:
        nombres = sorted(os.listdir(TRABAJOS))
    except OSError:
        return res
    for n in nombres:
        if n.endswith(".json"):
            try:
                with open(os.path.join(TRABAJOS, n), encoding="utf-8") as f:
                    res.append(json.load(f))
            except (OSError, ValueError):
                pass
    return res


def estado_trabajo(j):
    vivo, st = estado_proc(j["pid"])
    if vivo and (j.get("starttime") is None or st == j["starttime"]):
        return "en marcha", None
    cola = (leer_cola(j["log"], 2048) or "")
    m = re.findall(r"\[larga\] FIN rc=(-?\d+)([^\n]*)", cola)
    if m:
        rc = int(m[-1][0])
        if "detenido" in m[-1][1]:
            return "detenido por ti (matar)", rc
        return ("terminó bien" if rc == 0 else "terminó con error (rc=%d)" % rc), rc
    return "DESAPARECIDO sin terminar (¿lo mató el sistema?)", None


def buscar_trabajo(texto):
    trabajos = cargar_trabajos()
    for j in reversed(trabajos):
        if j["id"] == texto or j["id"].startswith(texto) or j["nombre"] == texto:
            return j
    return None


def cmd_lanzar(args):
    cmd = list(args.cmd)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("Falta el comando.  Ejemplo:  python diagtermux.py lanzar -- ./mi_script.sh")
        return 2
    if len(cmd) > 1 and not shutil.which(cmd[0]):
        print("No encuentro el programa «%s»." % cmd[0])
        return 2
    os.makedirs(TRABAJOS, exist_ok=True)
    base = args.nombre or os.path.basename(cmd[0].split()[0])
    nombre = re.sub(r"[^\w.\-]", "_", base)[:24] or "trabajo"
    idt = time.strftime("%y%m%d-%H%M%S")
    log = os.path.join(TRABAJOS, "%s-%s.log" % (idt, nombre))
    if len(cmd) == 1:
        argv = ["bash", "-c", ENVOLTURA, "larga", "bash", "-c", cmd[0]]
    else:
        argv = ["bash", "-c", ENVOLTURA, "larga"] + cmd

    def bajar_prioridad():
        try:
            os.nice(args.nice)
        except OSError:
            pass

    with open(log, "ab", buffering=0) as lf:
        lf.write(("[larga] INICIO %s nice=%d dir=%s\n[larga] CMD %s\n\n" % (
            ahora(), args.nice, os.getcwd(), " ".join(cmd))).encode("utf-8", "replace"))
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=lf,
                             stderr=subprocess.STDOUT, start_new_session=True,
                             preexec_fn=bajar_prioridad if args.nice else None)
    time.sleep(0.2)
    job = {"id": "%s-%s" % (idt, nombre), "nombre": nombre, "cmd": " ".join(cmd),
           "inicio": ahora(), "pid": p.pid, "log": log, "nice": args.nice,
           "starttime": estado_proc(p.pid)[1]}
    with open(os.path.join(TRABAJOS, job["id"] + ".json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=1)

    print("Lanzado «%s»  (PID %d, prioridad nice %d)" % (nombre, p.pid, args.nice))
    print("  ID : " + job["id"])
    print("  Log: " + log)
    if not args.sin_wakelock:
        if shutil.which("termux-wake-lock"):
            try:
                subprocess.run(["termux-wake-lock"], timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("  Wake-lock activado (libéralo con termux-wake-unlock cuando no queden trabajos).")
            except (OSError, subprocess.SubprocessError):
                print("  No pude activar el wake-lock; pulsa «Acquire wakelock» en la notificación de Termux.")
        else:
            print("  (termux-wake-lock no está disponible aquí; sin wake-lock)")
    print("Ver:  python diagtermux.py ver %s     Parar:  python diagtermux.py matar %s"
          % (nombre, nombre))
    return 0


def cmd_trabajos(_args):
    trabajos = cargar_trabajos()
    if not trabajos:
        print("No hay trabajos lanzados.")
        return 0
    print("%-13s %-16s %7s  %-19s  %s" % ("ID", "NOMBRE", "PID", "INICIO", "ESTADO"))
    for j in trabajos[-30:]:
        est, _ = estado_trabajo(j)
        corto = "-".join(j["id"].split("-")[:2])     # aammdd-hhmmss; basta para «ver»/«matar»
        print("%-13s %-16s %7d  %-19s  %s" % (corto, j["nombre"][:16],
                                              j["pid"], j["inicio"], est))
    return 0


def cmd_ver(args):
    j = buscar_trabajo(args.id)
    if not j:
        print("No encuentro ese trabajo. Mira la lista con:  python diagtermux.py trabajos")
        return 1
    print("Estado: %s   (log: %s)" % (estado_trabajo(j)[0], j["log"]))
    print("-" * 60)
    try:
        with open(j["log"], "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            texto = f.read().decode("utf-8", "replace")
    except OSError:
        texto = ""
    print("\n".join(texto.splitlines()[-args.lineas:]))
    return 0


def cmd_matar(args):
    j = buscar_trabajo(args.id)
    if not j:
        print("No encuentro ese trabajo.")
        return 1
    if estado_trabajo(j)[0] != "en marcha":
        print("Ese trabajo ya no está en marcha.")
        return 0
    try:
        os.killpg(j["pid"], signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    for _ in range(20):
        if not estado_proc(j["pid"])[0]:
            break
        time.sleep(0.25)
    else:
        try:
            os.killpg(j["pid"], signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    anadir_linea(j["log"], "\n[larga] FIN rc=-15 (detenido con «matar» %s)" % ahora())
    print("Trabajo detenido.")
    return 0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Vigilante de Termux y lanzador de comandos largos.")
    ap.add_argument("--version", action="version", version="diagtermux " + VERSION)
    sub = ap.add_subparsers(dest="orden")

    p = sub.add_parser("vigilar", help="arranca el vigilante en segundo plano")
    p.add_argument("-i", "--intervalo", type=int, default=5,
                   help="segundos entre muestras (por defecto 5)")
    p.add_argument("--primer-plano", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(f=cmd_vigilar)

    sub.add_parser("parar", help="para el vigilante").set_defaults(f=cmd_parar)
    sub.add_parser("estado", help="foto del momento actual").set_defaults(f=cmd_estado)

    p = sub.add_parser("informe", help="analiza los cierres anormales registrados")
    p.add_argument("-u", "--ultimos", type=int, default=3,
                   help="cuántos cierres mostrar (por defecto 3)")
    p.set_defaults(f=cmd_informe)

    p = sub.add_parser("lanzar", help="lanza un comando largo desacoplado del terminal")
    p.add_argument("-n", "--nombre", help="nombre corto del trabajo")
    p.add_argument("--nice", type=int, default=10,
                   help="prioridad baja 0-19 (por defecto 10; 0 = normal)")
    p.add_argument("--sin-wakelock", action="store_true",
                   help="no activar termux-wake-lock")
    p.add_argument("cmd", nargs=argparse.REMAINDER,
                   help="comando (entre comillas si lleva tuberías) o programa y argumentos")
    p.set_defaults(f=cmd_lanzar)

    sub.add_parser("trabajos", help="lista los comandos lanzados").set_defaults(f=cmd_trabajos)

    p = sub.add_parser("ver", help="ultimas lineas del log de un trabajo")
    p.add_argument("id")
    p.add_argument("-l", "--lineas", type=int, default=20)
    p.set_defaults(f=cmd_ver)

    p = sub.add_parser("matar", help="detiene un trabajo")
    p.add_argument("id")
    p.set_defaults(f=cmd_matar)

    args = ap.parse_args()
    if not getattr(args, "f", None):
        ap.print_help()
        return 0
    return args.f(args)


if __name__ == "__main__":
    sys.exit(main())
