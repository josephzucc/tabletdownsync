#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
duck.py v1.2 - Busca en DuckDuckGo y usa Duck.ai desde Termux, manejando Mozilla Firefox
(Android) mediante la extensión "Duck Bridge" (duck.xpi).  Solo librería estándar.

  duck.py buscar "texto" [--pais es|ninguno|fr,de,it] [--periodo mes] [--paginas 3]
  duck.py ia "pregunta" [--web si|no|auto]
  duck.py chats [--origen auto|duckai|ddg|ambos] [--destino CARPETA] [--descargas]
  duck.py ver | historial | local TEXTO | abrir N | estado | ai-diag | shell
Manual: duckManualBono.txt
"""
import argparse, atexit, glob, json, shutil, os, re, shlex, sqlite3, subprocess, sys, textwrap, threading, time, unicodedata, uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote_plus

VERSION = "1.2"
CARPETA_PROGRAMA = os.path.dirname(os.path.abspath(__file__))   # aquí se guardan los chats exportados
HOME = os.environ.get("DUCK_HOME") or os.path.join(os.path.expanduser("~"), "duck")
DB = os.path.join(HOME, "duck.db")
LOG = os.path.join(HOME, "duck.log")
CFG = os.path.join(HOME, "config.json")
DEFAULT_CFG = {
    "puerto": 8771,                  # la extensión prueba 8771, 8772, 8773
    "firefox": "auto",               # paquete Android: auto | org.mozilla.firefox | org.mozilla.fenix | org.mozilla.firefox_beta
    "pais_defecto": "es",
    "retardo": 2,                    # segundos entre páginas / países
    "timeout": 30,                   # segundos esperando resultados
    "traer_firefox": True,           # poner Firefox en primer plano al empezar
    "volver_termux": True,           # volver a Termux al terminar
    "cerrar_pestana": False,         # cerrar la pestaña de DuckDuckGo al terminar
    "paginas_defecto": 3,            # páginas que carga "buscar" si no pones --paginas
    "ia_timeout": 150
}

# ------------------------------------------------------------------ utilidades
def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()
def slug(s, n=40):
    s = re.sub(r"[^a-z0-9]+", "_", norm(s)).strip("_")
    return (s[:n] or "x")
_loglock = threading.Lock()
def log(msg, show=False):
    line = f"{now()} {msg}"
    with _loglock:
        try:
            os.makedirs(HOME, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception: pass
        if show: print(msg, flush=True)

def sello():
    return datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

def rotar_log():
    """Al terminar, duck.log pasa a logs/duck_AAAA-MM-DD-HH-MM-SS.log (fecha/hora de su última escritura)."""
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG):
            d = os.path.join(HOME, "logs"); os.makedirs(d, exist_ok=True)
            st = datetime.fromtimestamp(os.path.getmtime(LOG)).strftime("%Y-%m-%d-%H-%M-%S")
            dst = os.path.join(d, f"duck_{st}.log"); k = 2
            while os.path.exists(dst):
                dst = os.path.join(d, f"duck_{st}_{k}.log"); k += 1
            os.replace(LOG, dst)
    except Exception: pass
atexit.register(rotar_log)

def load_cfg():
    os.makedirs(HOME, exist_ok=True)
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(CFG):
        try: cfg.update(json.load(open(CFG, encoding="utf-8")))
        except Exception as e: log(f"config.json ilegible: {e}", True)
    else:
        json.dump(DEFAULT_CFG, open(CFG, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    return cfg

# ------------------------------------------------------------------ países / fechas
# Región de DuckDuckGo (parámetro kl). wt-wt = sin región.
PAISES = {
    "es": "es-es", "espana": "es-es", "mx": "mx-es", "mexico": "mx-es", "ar": "ar-es", "argentina": "ar-es",
    "cl": "cl-es", "chile": "cl-es", "co": "co-es", "colombia": "co-es", "pe": "pe-es", "peru": "pe-es",
    "ve": "ve-es", "venezuela": "ve-es", "uy": "uy-es", "uruguay": "uy-es", "latam": "xl-es",
    "us": "us-en", "eeuu": "us-en", "usa": "us-en", "uk": "uk-en", "gb": "uk-en", "reinounido": "uk-en",
    "ie": "ie-en", "irlanda": "ie-en", "ca": "ca-en", "canada": "ca-en", "au": "au-en", "australia": "au-en",
    "fr": "fr-fr", "francia": "fr-fr", "de": "de-de", "alemania": "de-de", "it": "it-it", "italia": "it-it",
    "pt": "pt-pt", "portugal": "pt-pt", "br": "br-pt", "brasil": "br-pt", "ch": "ch-de", "suiza": "ch-de",
    "ch-fr": "ch-fr", "at": "at-de", "austria": "at-de", "nl": "nl-nl", "paisesbajos": "nl-nl", "holanda": "nl-nl",
    "be": "be-fr", "belgica": "be-fr", "be-nl": "be-nl", "se": "se-sv", "suecia": "se-sv", "no": "no-no",
    "noruega": "no-no", "dk": "dk-da", "dinamarca": "dk-da", "fi": "fi-fi", "finlandia": "fi-fi",
    "pl": "pl-pl", "polonia": "pl-pl", "gr": "gr-el", "grecia": "gr-el", "tr": "tr-tr", "turquia": "tr-tr",
    "jp": "jp-jp", "japon": "jp-jp", "cn": "cn-zh", "china": "cn-zh", "in": "in-en", "india": "in-en",
    "ru": "ru-ru", "rusia": "ru-ru", "za": "za-en", "sudafrica": "za-en",
}
SIN_PAIS = {"", "ninguno", "ninguna", "sin", "none", "no", "mundo", "wt", "todos", "cualquiera"}
NOMBRE_KL = {"wt-wt": "Sin país"}
def kl_list(pais, cfg):
    """pais: None -> defecto; 'ninguno' -> wt-wt; 'fr,de,it' -> varios; también acepta códigos kl crudos (xx-yy)."""
    if pais is None: pais = cfg.get("pais_defecto", "es")
    out = []
    for p in re.split(r"[,; ]+", pais.strip()):
        k = norm(p).replace(" ", "")
        if k in SIN_PAIS: kl = "wt-wt"
        elif k in PAISES: kl = PAISES[k]
        elif re.fullmatch(r"[a-z]{2}-[a-z]{2}", k): kl = k
        else: raise SystemExit(f"País no reconocido: '{p}'. Usa códigos como es, fr, de, it, mx, us, uk... o 'ninguno'. Ver: duck.py paises")
        if kl not in out: out.append(kl)
    return out or ["wt-wt"]

def parse_fecha(s):
    s = norm(s)
    if s in ("hoy",): return date.today()
    if s in ("ayer",): return date.today() - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: pass
    raise SystemExit(f"Fecha no válida: '{s}'. Formato: AAAA-MM-DD o DD/MM/AAAA")
def df_param(a):
    """Devuelve (df, etiqueta). df: d,w,m,y o AAAA-MM-DD..AAAA-MM-DD"""
    if getattr(a, "ultimo_mes", False): return "m", "último mes"
    per = norm(getattr(a, "periodo", None) or "")
    if per in ("dia", "d", "24h", "hoy"): return "d", "último día"
    if per in ("semana", "w", "7d"): return "w", "última semana"
    if per in ("mes", "m", "30d"): return "m", "último mes"
    if per in ("ano", "año", "y", "anio", "365d"): return "y", "último año"
    if per not in ("", "todo", "siempre", "cualquiera"): raise SystemExit(f"--periodo desconocido: {per} (dia, semana, mes, año, todo)")
    f, d, h = getattr(a, "fecha", None), getattr(a, "desde", None), getattr(a, "hasta", None)
    if f:
        x = parse_fecha(f); return f"{x}..{x}", f"el {x}"
    if d or h:
        x = parse_fecha(d) if d else date(2000, 1, 1); y = parse_fecha(h) if h else date.today()
        if x > y: x, y = y, x
        return f"{x}..{y}", f"{x} a {y}"
    return None, "cualquier fecha"

def build_url(q, kl, df, segura=None):
    u = f"https://duckduckgo.com/?q={quote_plus(q)}&kl={kl}&ia=web"
    if df: u += f"&df={df}"
    if segura: u += "&kp=" + {"estricta": "1", "moderada": "-1", "off": "-2", "no": "-2"}.get(norm(segura), "-1")
    return u

# ------------------------------------------------------------------ base de datos
def db():
    os.makedirs(HOME, exist_ok=True)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.create_function("norm", 1, norm)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS busquedas(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, consulta TEXT, enviada TEXT, paises TEXT, fecha TEXT,
      paginas INTEGER, total INTEGER, estado TEXT, urls TEXT, filtros TEXT);
    CREATE TABLE IF NOT EXISTS resultados(
      id INTEGER PRIMARY KEY AUTOINCREMENT, busqueda_id INTEGER, kl TEXT, pagina INTEGER, pos INTEGER, orden INTEGER,
      titulo TEXT, url TEXT, sitio TEXT, url_mostrada TEXT, fecha_rel TEXT, extracto TEXT, ts TEXT,
      UNIQUE(busqueda_id, url));
    CREATE TABLE IF NOT EXISTS ia(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, prompt TEXT, modelo TEXT, web_pedida TEXT, web_usada INTEGER,
      respuesta TEXT, razonamiento TEXT, consultas TEXT, fuentes TEXT, segundos INTEGER, interruptor TEXT);
    """)
    return c

# ------------------------------------------------------------------ servidor local
class State:
    def __init__(self):
        self.job = None; self.delivered = False; self.result = None
        self.last_poll = 0.0; self.polls = 0; self.cv = threading.Condition()
        self.show_logs = True

S = State()
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in (("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Methods", "GET,POST,OPTIONS"), ("Access-Control-Allow-Headers", "Content-Type")):
            self.send_header(k, v)
        self.end_headers()
    def do_GET(self):
        if self.path.startswith("/ping"): return self._send({"app": "duck", "version": VERSION})
        if self.path.startswith("/poll"):
            with S.cv:
                S.last_poll = time.time(); S.polls += 1
                if S.job and not S.delivered:
                    S.delivered = True; return self._send(S.job)
            return self._send({})
        self._send({"error": "ruta"}, 404)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: data = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception: data = {}
        if self.path.startswith("/log"):
            log(f"[ext] {data.get('msg')}", S.show_logs); return self._send({"ok": True})
        if self.path.startswith("/result"):
            with S.cv:
                S.result = data; S.cv.notify_all()
            return self._send({"ok": True})
        self._send({"error": "ruta"}, 404)

def start_server(cfg):
    port = int(cfg["puerto"])
    for p in (port, port + 1, port + 2):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            log(f"servidor local en 127.0.0.1:{p}")
            return srv, p
        except OSError: continue
    raise SystemExit(f"No puedo abrir el puerto {port} (¿otra copia de duck.py abierta?). Cierra la otra o cambia 'puerto' en {CFG}")

# ------------------------------------------------------------------ Firefox / Android
def sh(cmd, timeout=15):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e: return None
def firefox_pkg(cfg):
    p = cfg.get("firefox", "auto")
    if p and p != "auto": return p
    r = sh(["pm", "list", "packages"])
    pk = []
    if r and r.stdout:
        pk = [l.split(":", 1)[1].strip() for l in r.stdout.splitlines() if re.search(r"mozilla|fenix|firefox", l, re.I)]
    for pref in ("org.mozilla.firefox", "org.mozilla.fenix", "org.mozilla.firefox_beta", "org.mozilla.fennec_aurora"):
        if pref in pk: return pref
    return pk[0] if pk else "org.mozilla.firefox"
def traer_firefox(cfg):
    pkg = firefox_pkg(cfg)
    r = sh(["am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-p", pkg])
    if not r: log("am no disponible (¿no estás en Termux?)")
    return pkg
def abrir_url_firefox(url, cfg):
    pkg = firefox_pkg(cfg)
    r = sh(["am", "start", "-a", "android.intent.action.VIEW", "-d", url, "-p", pkg])
    if r is None or r.returncode != 0:
        r2 = sh(["termux-open-url", url])
        if r2 is None or r2.returncode != 0:
            print(f"No pude abrir Firefox automáticamente. Abre a mano:\n  {url}"); return False
    return True
def volver_termux():
    sh(["am", "start", "-n", "com.termux/com.termux.app.TermuxActivity"])

# ------------------------------------------------------------------ ejecutar trabajo
def esperar_extension(cfg, espera=45):
    if time.time() - S.last_poll < 8: return True
    print("Esperando a la extensión Duck Bridge en Firefox...", flush=True)
    pkg = traer_firefox(cfg)
    t0 = time.time()
    while time.time() - t0 < espera:
        if time.time() - S.last_poll < 8: return True
        time.sleep(0.5)
    print(f"La extensión no responde (Firefox: {pkg}). ¿Está instalada duck.xpi y Firefox abierto? Ver 'duck.py estado' y el manual.")
    return False

def run_job(job, cfg, limite=None):
    if not esperar_extension(cfg): return None
    if cfg.get("traer_firefox", True): traer_firefox(cfg)
    job["id"] = uuid.uuid4().hex[:8]
    with S.cv:
        S.result = None; S.delivered = False; S.job = job
    log(f"trabajo {job['id']}: {json.dumps({k: v for k, v in job.items() if k != 'prompt'}, ensure_ascii=False)[:600]}")
    t0 = time.time(); limite = limite or 900
    with S.cv:
        while S.result is None and time.time() - t0 < limite:
            S.cv.wait(timeout=1.0)
        res = S.result; S.job = None
    if cfg.get("volver_termux", True): volver_termux()
    if res is None:
        print("Tiempo agotado esperando a la extensión."); return None
    log("resultado crudo: " + json.dumps(res, ensure_ascii=False)[:4000])
    return res

# ------------------------------------------------------------------ búsqueda
def fmt_lista(rows, ancho=100):
    out = []
    for i, r in enumerate(rows, 1):
        titulo = textwrap.shorten(r["titulo"] or "(sin título)", ancho, placeholder="...")
        out.append(f"{i:>3}. {titulo}")
        out.append(f"     {r['url']}")
        meta = " | ".join(x for x in (r["sitio"], r["fecha_rel"], f"pág {r['pagina']}", r["kl"]) if x)
        if meta: out.append(f"     [{meta}]")
        if r["extracto"]: out.extend("     " + l for l in textwrap.wrap(r["extracto"], 92)[:3])
        out.append("")
    return "\n".join(out)

def guardar_txt(bid, c):
    b = c.execute("SELECT * FROM busquedas WHERE id=?", (bid,)).fetchone()
    rows = c.execute("SELECT * FROM resultados WHERE busqueda_id=? ORDER BY orden", (bid,)).fetchall()
    d = os.path.join(HOME, "txt"); os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, f"busqueda_{bid:04d}_{slug(b['consulta'])}.txt")
    cab = [f"BÚSQUEDA {bid}  ({b['ts']})", f"Consulta : {b['consulta']}", f"Enviada  : {b['enviada']}",
           f"Países   : {b['paises']}", f"Fecha    : {b['fecha']}", f"Páginas  : {b['paginas']}   Resultados: {len(rows)}",
           f"Filtros vistos en DuckDuckGo: {b['filtros']}", "=" * 100, ""]
    open(fn, "w", encoding="utf-8").write("\n".join(cab) + fmt_lista(rows) + "\n")
    return fn

def cmd_buscar(a, cfg):
    q = " ".join(a.consulta).strip()
    if not q: raise SystemExit("Falta el texto a buscar.")
    if a.sitio: q += " site:" + a.sitio.replace("https://", "").replace("http://", "").strip("/")
    if a.sin_pais: pais = "ninguno"
    elif a.paises: pais = a.paises
    else: pais = a.pais
    kls = kl_list(pais, cfg)
    df, df_txt = df_param(a)
    runs = [{"kl": kl, "url": build_url(q, kl, df, a.segura)} for kl in kls]
    npag = max(1, a.paginas or cfg.get('paginas_defecto', 3))
    a.paginas = npag
    minres = max(0, a.resultados or 0)
    print(f"Buscando: {q}\n  País/región: {', '.join(NOMBRE_KL.get(k, k) for k in kls)} | Fecha: {df_txt} | Páginas: {npag}" + (f" | Mínimo de resultados: {minres}" if minres else ""), flush=True)
    job = {"type": "search", "runs": runs, "pages": npag, "delay": cfg["retardo"], "timeout": cfg["timeout"],
           "min_results": minres, "close_tab": cfg.get("cerrar_pestana", False), "continue": bool(getattr(a, "_continuar", False))}
    res = run_job(job, cfg)
    if not res: return None
    if res.get("error"): print("Error de la extensión:", res["error"]); return None
    c = db(); total = 0; orden = 0; vistos = {}
    cur = c.execute("INSERT INTO busquedas(ts,consulta,enviada,paises,fecha,paginas,total,estado,urls,filtros) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (now(), q, q, ",".join(kls), df_txt, npag, 0, "ok", json.dumps([r["url"] for r in runs]), ""))
    bid = cur.lastrowid; filt = []; estados = []
    for run in res.get("runs", []):
        kl = run["kl"]; f = run.get("filtros") or {}
        filt.append(f"{kl}: región='{f.get('region','?')}' fecha='{f.get('fecha','?')}'")
        if run.get("status") != "ok": estados.append(f"{kl}:{run.get('status')}")
        for pg in run.get("pages", []):
            if pg["page"] < a.desde_pagina: continue
            for it in pg["items"]:
                u = it["url"]
                if u in vistos:
                    c.execute("UPDATE resultados SET kl = kl || ',' || ? WHERE id=?", (kl, vistos[u])); continue
                orden += 1
                try:
                    cc = c.execute("INSERT INTO resultados(busqueda_id,kl,pagina,pos,orden,titulo,url,sitio,url_mostrada,fecha_rel,extracto,ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (bid, kl, it["pagina"], it.get("pos"), orden, it["titulo"], u, it.get("sitio"), it.get("url_mostrada"), it.get("fecha_rel"), it.get("extracto"), now()))
                    vistos[u] = cc.lastrowid; total += 1
                except sqlite3.IntegrityError: pass
    c.execute("UPDATE busquedas SET total=?, estado=?, filtros=? WHERE id=?", (total, "ok" if not estados else ";".join(estados), " | ".join(filt), bid))
    c.commit()
    rows = c.execute("SELECT * FROM resultados WHERE busqueda_id=? ORDER BY orden", (bid,)).fetchall()
    fn = guardar_txt(bid, c)
    print()
    if rows: print(fmt_lista(rows))
    print(f"Guardados {total} resultados en la base local (búsqueda nº {bid}).\nFiltros que mostraba DuckDuckGo -> {' | '.join(filt)}\nTXT: {fn}")
    if estados:
        print("Avisos:", ", ".join(estados), "(captcha = resuélvelo en Firefox y repite; sin_resultados = nada para esos filtros)")
    if a.abrir and rows:
        for n in a.abrir: abrir_n(c, bid, n, cfg)
    return bid

# ------------------------------------------------------------------ consultas locales
def ultima_busqueda(c):
    r = c.execute("SELECT id FROM busquedas ORDER BY id DESC LIMIT 1").fetchone()
    return r["id"] if r else None
def cmd_historial(a, cfg):
    c = db()
    rows = c.execute("SELECT * FROM busquedas ORDER BY id DESC LIMIT ?", (a.n,)).fetchall()
    if not rows: print("Sin búsquedas guardadas."); return
    print(f"{'ID':>4}  {'Fecha':<19}  {'Res':>4}  {'Países':<12} {'Fecha filtro':<18} Consulta")
    for r in rows:
        print(f"{r['id']:>4}  {r['ts']:<19}  {r['total']:>4}  {textwrap.shorten(r['paises'],12):<12} {textwrap.shorten(r['fecha'],18):<18} {r['consulta']}")
    ia = c.execute("SELECT id, ts, prompt FROM ia ORDER BY id DESC LIMIT 5").fetchall()
    if ia:
        print("\nÚltimas consultas a Duck.ai (duck.py ia-ver ID):")
        for r in ia: print(f"{r['id']:>4}  {r['ts']:<19}  {textwrap.shorten(r['prompt'], 70)}")
def cmd_ver(a, cfg):
    c = db(); bid = a.id or ultima_busqueda(c)
    if not bid: print("No hay búsquedas."); return
    q = "SELECT * FROM resultados WHERE busqueda_id=?"; p = [bid]
    if a.pagina: q += " AND pagina=?"; p.append(a.pagina)
    rows = c.execute(q + " ORDER BY orden", p).fetchall()
    b = c.execute("SELECT * FROM busquedas WHERE id=?", (bid,)).fetchone()
    print(f"Búsqueda {bid}: {b['consulta']}  [{b['paises']} | {b['fecha']}]  {len(rows)} resultados\n")
    print(fmt_lista(rows))
def cmd_local(a, cfg):
    c = db(); terms = [norm(t) for t in " ".join(a.texto).split()]
    q = "SELECT r.*, b.consulta FROM resultados r JOIN busquedas b ON b.id=r.busqueda_id WHERE " + " AND ".join(["norm(coalesce(r.titulo,'')||' '||coalesce(r.extracto,'')||' '||r.url||' '||coalesce(r.sitio,'')) LIKE ?"] * len(terms))
    rows = c.execute(q + " ORDER BY r.id DESC LIMIT ?", [f"%{t}%" for t in terms] + [a.limite]).fetchall()
    print(f"{len(rows)} coincidencias en la base local:\n")
    for r in rows:
        print(f"[búsq {r['busqueda_id']} nº{r['orden']}] {r['titulo']}\n     {r['url']}\n     (consulta: {r['consulta']})\n")
    ia = c.execute("SELECT id, prompt, respuesta FROM ia WHERE " + " AND ".join(["norm(prompt||' '||respuesta) LIKE ?"] * len(terms)) + " ORDER BY id DESC LIMIT 10", [f"%{t}%" for t in terms]).fetchall()
    for r in ia: print(f"[Duck.ai {r['id']}] {textwrap.shorten(r['prompt'], 80)}")
def abrir_n(c, bid, n, cfg):
    r = c.execute("SELECT url, titulo FROM resultados WHERE busqueda_id=? AND orden=?", (bid, n)).fetchone()
    if not r: print(f"No existe el resultado {n} en la búsqueda {bid}."); return
    print(f"Abriendo en Firefox: {r['titulo']}\n  {r['url']}"); abrir_url_firefox(r["url"], cfg)
def cmd_abrir(a, cfg):
    c = db()
    for x in a.destino:
        if re.match(r"^(https?://|about:)", x): print("Abriendo", x); abrir_url_firefox(x, cfg)
        elif re.fullmatch(r"\d+", x): abrir_n(c, a.busqueda or ultima_busqueda(c), int(x), cfg)
        elif "." in x and " " not in x: abrir_url_firefox("https://" + x, cfg)
        else: print(f"No entiendo '{x}': número de resultado o URL.")
def cmd_abrir_busqueda(a, cfg):
    c = db(); b = c.execute("SELECT * FROM busquedas WHERE id=?", (a.id or ultima_busqueda(c),)).fetchone()
    for u in json.loads(b["urls"]): abrir_url_firefox(u, cfg)

# ------------------------------------------------------------------ Duck.ai
def cmd_ia(a, cfg, diag=False):
    prompt = " ".join(a.pregunta).strip() if not diag else ""
    web = {"si": "on", "sí": "on", "on": "on", "no": "off", "off": "off", "auto": None}.get(norm(a.web), "on")
    job = {"type": "ai", "url": "https://duck.ai/", "prompt": prompt, "web": web, "new_chat": True,
           "timeout": cfg["ia_timeout"], "diag": diag, "close_tab": cfg.get("cerrar_pestana", False)}
    print(f"Duck.ai ({'diagnóstico' if diag else 'consulta'}): {prompt[:100]}  | búsqueda web: {a.web if not diag else '-'}", flush=True)
    res = run_job(job, cfg, limite=cfg["ia_timeout"] + 120)
    if not res: return
    if diag:
        print(json.dumps(res.get("web_interruptor"), ensure_ascii=False, indent=2))
        fn = os.path.join(HOME, f"ai_diag_{sello()}.json")
        json.dump(res, open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("Diagnóstico completo guardado en", fn, "(envíaselo a Claude si falla el interruptor web)"); return
    if res.get("error") and not res.get("respuesta"):
        print("Error:", res["error"]); print(json.dumps({k: v for k, v in res.items() if k in ("titulo", "texto_pagina", "web_interruptor")}, ensure_ascii=False, indent=2)); return
    w = res.get("web_interruptor") or {}
    print(f"\nModelo: {res.get('modelo') or '?'} | Tiempo: {res.get('segundos')} s")
    est = f"interruptor 'búsqueda web': antes={w.get('estado_antes')}, después={w.get('estado_despues')}, pulsado={w.get('clic')}"
    print(est + (f" ({w['aviso']})" if w.get("aviso") else ""))
    usada = (res.get("web") or {}).get("usada")
    print("Búsqueda web usada en esta respuesta:", "SÍ" if usada else "NO")
    for cq in (res.get("web") or {}).get("consultas", []): print("  consulta web:", cq.get("consulta"))
    if res.get("aviso"): print("AVISO:", res["aviso"])
    print("\n" + "=" * 80 + "\n" + (res.get("respuesta") or "(vacía)") + "\n" + "=" * 80)
    if res.get("fuentes"):
        print("\nFuentes:")
        for i, f in enumerate(res["fuentes"], 1): print(f"{i:>3}. {f['titulo']}\n     {f['url']}")
    c = db()
    cur = c.execute("INSERT INTO ia(ts,prompt,modelo,web_pedida,web_usada,respuesta,razonamiento,consultas,fuentes,segundos,interruptor) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (now(), prompt, res.get("modelo"), a.web, 1 if usada else 0, res.get("respuesta"), res.get("razonamiento"),
                     json.dumps((res.get("web") or {}).get("consultas", []), ensure_ascii=False), json.dumps(res.get("fuentes", []), ensure_ascii=False),
                     res.get("segundos"), json.dumps(w, ensure_ascii=False)))
    c.commit(); iid = cur.lastrowid
    d = os.path.join(HOME, "txt"); os.makedirs(d, exist_ok=True)
    fn = os.path.join(d, f"ia_{iid:04d}_{slug(prompt)}.txt")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(f"DUCK.AI {iid} ({now()})\nModelo: {res.get('modelo')}\nBúsqueda web usada: {'sí' if usada else 'no'}\n\nPREGUNTA:\n{prompt}\n\nRESPUESTA:\n{res.get('respuesta')}\n\n")
        if res.get("fuentes"): f.write("FUENTES:\n" + "\n".join(f"{i}. {x['titulo']} - {x['url']}" for i, x in enumerate(res["fuentes"], 1)) + "\n")
    print(f"\nGuardado como Duck.ai nº {iid}: {fn}")
def cmd_ia_ver(a, cfg):
    c = db(); r = c.execute("SELECT * FROM ia WHERE id=?", (a.id,)).fetchone() if a.id else c.execute("SELECT * FROM ia ORDER BY id DESC LIMIT 1").fetchone()
    if not r: print("No hay consultas a Duck.ai guardadas."); return
    print(f"Duck.ai {r['id']} ({r['ts']}) modelo {r['modelo']} web_usada={bool(r['web_usada'])}\n\nPREGUNTA: {r['prompt']}\n\n{r['respuesta']}\n")
    for i, f in enumerate(json.loads(r["fuentes"] or "[]"), 1): print(f"{i:>3}. {f['titulo']}\n     {f['url']}")

# ------------------------------------------------------------------ exportar chats de Duck.ai
URL_DUCKAI = "https://duck.ai/"
URL_DDG_CHAT = "https://duckduckgo.com/?q=DuckDuckGo+AI+Chat&ia=chat&duckai=1"

def lista_chats(sc):
    """savedAIChats puede ser {"chats":[...]} o directamente una lista."""
    if isinstance(sc, dict):
        for k in ("chats", "savedChats", "conversations"):
            if isinstance(sc.get(k), list): return sc[k]
    return sc if isinstance(sc, list) else []

def id_chat(c, i):
    return str(c.get("chatId") or c.get("id") or c.get("uuid") or f"{c.get('title','')}|{i}")

def chats_de_idb(datos):
    """Convierte lo guardado en el almacén IndexedDB 'saved-chats' de Duck.ai en chats {chatId, title, messages...}."""
    out = []
    for it in datos if isinstance(datos, list) else []:
        x = it
        if isinstance(x, str):
            try: x = json.loads(x)
            except Exception: continue
        if not isinstance(x, dict): continue
        if not isinstance(x.get("messages"), list):
            for k in ("chat", "value", "data", "savedChat", "conversation"):
                v = x.get(k)
                if isinstance(v, str):
                    try: v = json.loads(v)
                    except Exception: v = None
                if isinstance(v, dict) and isinstance(v.get("messages"), list):
                    x = {**{kk: vv for kk, vv in x.items() if kk != k}, **v}; break
        if isinstance(x.get("messages"), list): out.append(x)
    return out

def fecha_chat(c):
    v = c.get("lastEdit") or c.get("updatedAt") or c.get("createdAt") or ""
    if isinstance(v, (int, float)) and v > 0:
        v = datetime.fromtimestamp(v / 1000 if v > 1e11 else v).strftime("%Y-%m-%dT%H:%M:%S")
    return str(v)[:19].replace("T", " ")

RE_EXPORT = re.compile(r"^\d{4}-\d\d-\d\d-\d\d-\d\d-\d\d\.json$")

def cmd_chats(a, cfg):
    origen = norm(a.origen or "auto")
    urls = {"duckai": [URL_DUCKAI], "ddg": [URL_DDG_CHAT]}.get(origen, [URL_DUCKAI, URL_DDG_CHAT])
    job = {"type": "export_chats", "urls": urls, "parar_si_hay": origen == "auto", "espera_ms": 2500,
           "max_bytes": 80 * 1024 * 1024, "close_tab": cfg.get("cerrar_pestana", False)}
    print(f"Exportando los chats de Duck.ai guardados en Firefox ({', '.join(urls)})...", flush=True)
    res = run_job(job, cfg, limite=300)
    if not res: return
    if res.get("error"): print("Error de la extensión:", res["error"])
    porid, origenes = {}, []
    cuenta = {"localStorage": 0, "indexedDB": 0}
    diag = {"fecha": now(), "version_duck": VERSION, "origenes": []}

    def meter(c, i, de):
        k = id_chat(c, i)
        prev = porid.get(k)
        if prev is None or (fecha_chat(c), len(c.get("messages") or [])) > (fecha_chat(prev), len(prev.get("messages") or [])):
            porid[k] = c
        if prev is None: cuenta[de] += 1

    for o in res.get("origenes", []):
        sc = o.get("savedAIChats")
        lc = lista_chats(sc)
        for i, c in enumerate(lc):
            if isinstance(c, dict): meter(c, i, "localStorage")
        idb = o.get("idb") or {}
        alm_diag = []
        for b in idb.get("bases") or []:
            for st in b.get("almacenes") or []:
                reconocidos = chats_de_idb(st.get("datos"))
                for i, c in enumerate(reconocidos): meter(c, i, "indexedDB")
                alm_diag.append({"base": b.get("nombre"), "almacen": st.get("nombre"), "n": st.get("n"),
                                 "copiado": st.get("datos") is not None, "chats_reconocidos": len(reconocidos),
                                 "forma": st.get("forma"), "aviso": st.get("aviso"), "error": st.get("error")})
        if lc or any(x["chats_reconocidos"] for x in alm_diag): origenes.append(o.get("origen") or o.get("url_pedida"))
        diag["origenes"].append({
            "url": o.get("url_pedida"), "origen": o.get("origen"), "ok": o.get("ok"), "error": o.get("error"),
            "localStorage_savedAIChats": len(lc),
            "campos_chat": sorted({k for c in lc[:50] if isinstance(c, dict) for k in c}),
            "localStorage": o.get("claves"), "indexedDB_error": idb.get("error"), "indexedDB": alm_diag})
    chats = sorted(porid.values(), key=fecha_chat, reverse=True)
    carpeta = os.path.expanduser(a.destino) if a.destino else CARPETA_PROGRAMA
    os.makedirs(carpeta, exist_ok=True)
    st = sello()
    fdiag = os.path.join(carpeta, f"{st}duckai_diag.json")
    diag["total_chats"] = len(chats); diag["de_localStorage"] = cuenta["localStorage"]; diag["de_indexedDB"] = cuenta["indexedDB"]
    diag["campos_chat_indexedDB"] = sorted({k for c in chats[:80] for k in c})
    diag["campos_mensaje"] = sorted({k for c in chats[:80] for m in (c.get("messages") or [])[:20] if isinstance(m, dict) for k in m})
    json.dump(diag, open(fdiag, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    if not chats:
        print("\nNo he encontrado chats guardados en Firefox.")
        print("  - ¿Usas Duck.ai en ESTE Firefox con el historial activado (Ajustes de Duck.ai > historial de chats)?")
        print("  - Firefox borra estos datos si limpias datos de navegación o usas pestañas privadas.")
        print(f"Diagnóstico (sin el texto de tus chats): {fdiag}  -> envíaselo a Claude.")
        log(f"chats: 0 chats; diagnóstico {fdiag}"); return
    # comparar con la exportación anterior (en esta carpeta o en la antigua ~/duck/chats)
    previos = sorted([p for p in glob.glob(os.path.join(glob.escape(carpeta), "*.json")) if RE_EXPORT.match(os.path.basename(p))],
                     key=os.path.basename)
    previos = previos or sorted(glob.glob(os.path.join(HOME, "chats", "duckai_chats_*.json")))
    antes = {}
    if previos:
        try:
            for i, c in enumerate(lista_chats(json.load(open(previos[-1], encoding="utf-8")))):
                if isinstance(c, dict): antes[id_chat(c, i)] = len(c.get("messages") or [])
        except Exception: pass
    salida = {"exportado": datetime.now().astimezone().isoformat(), "herramienta": f"duck.py {VERSION}",
              "origenes": origenes, "chats": chats}
    fn = os.path.join(carpeta, f"{st}.json")
    json.dump(salida, open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ids = [id_chat(c, i) for i, c in enumerate(chats)]
    nuevos = [c for k, c in zip(ids, chats) if k not in antes]
    cambiados = [c for k, c in zip(ids, chats) if k in antes and len(c.get("messages") or []) != antes[k]]
    print(f"\n{len(chats)} chats exportados ({cuenta['indexedDB']} del almacén actual de Duck.ai, "
          f"{cuenta['localStorage']} del almacén antiguo)" +
          (f"; {len(nuevos)} nuevos y {len(cambiados)} con mensajes nuevos desde la exportación anterior" if previos else "") + ":")
    for c in chats[:15]:
        print(f"  {fecha_chat(c)[:16]:<16}  {textwrap.shorten(str(c.get('title') or '(sin título)'), 60):<60}  {len(c.get('messages') or []):>3} mensajes  {c.get('model') or ''}")
    if len(chats) > 15: print(f"  ... y {len(chats) - 15} más")
    print(f"\nGuardado: {fn}\nDiagnóstico: {fdiag}")
    if a.descargas:
        for d in (os.path.expanduser("~/storage/downloads"), os.path.expanduser("~/storage/shared/Download")):
            if os.path.isdir(d) and os.path.abspath(d) != os.path.abspath(carpeta):
                shutil.copy2(fn, d); print(f"Copia en Descargas: {os.path.join(d, os.path.basename(fn))}"); break
    print("Para buscar en ellos:  python buscarai.py " + fn)
    log(f"chats: {len(chats)} exportados en {fn}")

# ------------------------------------------------------------------ estado / paises
def cmd_estado(a, cfg):
    print(f"duck.py v{VERSION} | datos en {HOME}\nFirefox (paquete): {firefox_pkg(cfg)}")
    t0 = time.time()
    while time.time() - t0 < 25 and not S.polls:
        if not hasattr(S, "_t"): S._t = 1; traer_firefox(cfg); print("Esperando a la extensión (hasta 25 s)...", flush=True)
        time.sleep(0.5)
    print("Extensión Duck Bridge:", "CONECTADA" if S.polls else "NO responde (¿instalada? ¿Firefox abierto?)")
    c = db(); print("Búsquedas guardadas:", c.execute("select count(*) from busquedas").fetchone()[0], "| resultados:", c.execute("select count(*) from resultados").fetchone()[0], "| Duck.ai:", c.execute("select count(*) from ia").fetchone()[0])
def cmd_paises(a, cfg):
    print("Códigos de país admitidos (región kl de DuckDuckGo):")
    vistos = {}
    for k, v in sorted(PAISES.items()): vistos.setdefault(v, []).append(k)
    for v, ks in sorted(vistos.items()): print(f"  {v:<6} <- {', '.join(ks)}")
    print("  wt-wt  <- ninguno (sin país)\nTambién vale cualquier código kl crudo (p.ej. fr-fr).")

# ------------------------------------------------------------------ parser
def build_parser():
    p = argparse.ArgumentParser(prog="duck.py", description="DuckDuckGo y Duck.ai desde Termux con Firefox. Manual: duckManualBono.txt")
    sp = p.add_subparsers(dest="cmd")
    b = sp.add_parser("buscar", aliases=["b"], help="buscar en DuckDuckGo")
    b.add_argument("consulta", nargs="+")
    b.add_argument("--pais", default=None, help="es (defecto) | ninguno | lista: fr,de,it")
    b.add_argument("--paises", default=None, help="lista de países: fr,de,it")
    b.add_argument("--sin-pais", action="store_true", help="sin región")
    b.add_argument("--periodo", help="dia | semana | mes | año | todo")
    b.add_argument("--ultimo-mes", action="store_true")
    b.add_argument("--fecha", help="un día concreto AAAA-MM-DD (también hoy/ayer)")
    b.add_argument("--desde", help="inicio del rango"); b.add_argument("--hasta", help="fin del rango")
    b.add_argument("--sitio", help="limitar a un dominio (site:)")
    b.add_argument("--paginas", type=int, default=None, help="páginas a cargar pulsando 'Más resultados' (defecto 3, ver config.json)")
    b.add_argument("--resultados", type=int, default=0, help="seguir cargando páginas hasta tener al menos N resultados (máx. 30 páginas)")
    b.add_argument("--desde-pagina", type=int, default=1, help="guardar solo desde esta página (carga todas igualmente)")
    b.add_argument("--segura", help="estricta | moderada | off")
    b.add_argument("--abrir", type=int, nargs="*", help="abrir en Firefox estos números de resultado al acabar")
    h = sp.add_parser("historial", aliases=["h"]); h.add_argument("-n", type=int, default=20)
    v = sp.add_parser("ver"); v.add_argument("id", nargs="?", type=int); v.add_argument("--pagina", type=int)
    l = sp.add_parser("local", help="buscar en lo guardado"); l.add_argument("texto", nargs="+"); l.add_argument("--limite", type=int, default=50)
    o = sp.add_parser("abrir", help="abrir en Firefox: nº de resultado o URL"); o.add_argument("destino", nargs="+"); o.add_argument("--busqueda", type=int)
    ob = sp.add_parser("abrir-busqueda", help="abrir la búsqueda en DuckDuckGo en Firefox"); ob.add_argument("id", nargs="?", type=int)
    i = sp.add_parser("ia", help="preguntar a Duck.ai"); i.add_argument("pregunta", nargs="+"); i.add_argument("--web", default="si", help="si (defecto, la activa) | no | auto (no toca)")
    iv = sp.add_parser("ia-ver"); iv.add_argument("id", nargs="?", type=int)
    d = sp.add_parser("ai-diag", help="volcar el menú de Herramientas de Duck.ai"); d.add_argument("--web", default="auto")
    ch = sp.add_parser("chats", aliases=["ia-exportar"], help="exportar a JSON los chats de Duck.ai guardados en Firefox")
    ch.add_argument("--origen", default="auto", help="auto (duck.ai y, si no hay chats, duckduckgo.com) | duckai | ddg | ambos")
    ch.add_argument("--destino", help="carpeta donde guardar (defecto: la carpeta de duck.py)")
    ch.add_argument("--descargas", action="store_true", help="copiar también el JSON a Descargas")
    sp.add_parser("estado"); sp.add_parser("paises"); sp.add_parser("shell")
    return p

def dispatch(a, cfg):
    c = a.cmd
    if c in ("buscar", "b"): return cmd_buscar(a, cfg)
    if c in ("historial", "h"): return cmd_historial(a, cfg)
    if c == "ver": return cmd_ver(a, cfg)
    if c == "local": return cmd_local(a, cfg)
    if c == "abrir": return cmd_abrir(a, cfg)
    if c == "abrir-busqueda": return cmd_abrir_busqueda(a, cfg)
    if c == "ia": return cmd_ia(a, cfg)
    if c == "ia-ver": return cmd_ia_ver(a, cfg)
    if c == "ai-diag": return cmd_ia(a, cfg, diag=True)
    if c in ("chats", "ia-exportar"): return cmd_chats(a, cfg)
    if c == "estado": return cmd_estado(a, cfg)
    if c == "paises": return cmd_paises(a, cfg)

SHELL_HELP = """Comandos (como en la línea de órdenes, sin 'duck.py'):
  buscar TEXTO [--pais fr,de] [--periodo mes] [--paginas 3] [--sitio dominio]
  mas [N]         cargar N páginas más de la última búsqueda (misma pestaña de Firefox)
  ver [ID]  historial  local TEXTO  abrir N|URL  abrir-busqueda
  ia PREGUNTA [--web si|no|auto]   ia-ver [ID]   ai-diag
  chats [--origen auto|duckai|ddg|ambos] [--descargas]   exportar los chats de Duck.ai
  estado   ayuda   salir"""
def cmd_shell(p, cfg):
    print("duck.py shell. 'ayuda' para ver comandos, 'salir' para terminar.")
    if not esperar_extension(cfg): print("(seguirás sin extensión: solo consultas locales)")
    last = None
    while True:
        try: line = input("duck> ").strip()
        except (EOFError, KeyboardInterrupt): print(); break
        if not line: continue
        if line in ("salir", "exit", "q"): break
        if line in ("ayuda", "help", "?"): print(SHELL_HELP); continue
        try: parts = shlex.split(line)
        except ValueError as e: print("Comillas sin cerrar:", e); continue
        try:
            if parts[0] == "mas":
                if not last: print("Primero haz una búsqueda."); continue
                n = int(parts[1]) if len(parts) > 1 else 1
                a = last; a.paginas = a.paginas + n; a.desde_pagina = a.paginas - n + 1; a._continuar = True
                cmd_buscar(a, cfg); continue
            a = p.parse_args(parts)
            if a.cmd in ("buscar", "b"):
                a._continuar = False; r = cmd_buscar(a, cfg)
                if r: last = a
            elif a.cmd == "shell": print("Ya estás en el shell.")
            elif a.cmd: dispatch(a, cfg)
        except SystemExit as e:
            if e.code not in (0, None): print(e if isinstance(e.code, str) else "")
        except Exception as e:
            log(f"EXCEPCION shell: {e!r}", True)

def main():
    cfg = load_cfg(); p = build_parser(); a = p.parse_args()
    if not a.cmd: p.print_help(); return
    log(f"== duck.py v{VERSION} args={sys.argv[1:]}")
    if a.cmd in ("historial", "h", "ver", "local", "ia-ver", "paises"): return dispatch(a, cfg)
    if a.cmd in ("abrir", "abrir-busqueda"): return dispatch(a, cfg)
    srv, port = start_server(cfg)
    try:
        if a.cmd == "shell": cmd_shell(p, cfg)
        else: dispatch(a, cfg)
    except KeyboardInterrupt: print("\nInterrumpido.")
    finally: srv.shutdown()

if __name__ == "__main__":
    main()
