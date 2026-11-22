#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
empstat.py - Estadisticas de ofertas de empleo en Suiza (vb6, altsystem, cobol...) desde Termux. Solo biblioteca estandar.
Descarga el CSV publico diario https://storage.googleapis.com/jobboard-free/all_jobs.csv (unas 450 empresas; solo TITULOS,
sin descripcion), cuenta cuantas ofertas coinciden con cada tecnologia y guarda la historia para ver la evolucion.

  python3 empstat.py contar                    (tabla con la lista por defecto)
  python3 empstat.py contar --lista "vb6,cobol" --listar
  python3 empstat.py contar --regex "mainframe|jcl" --nombre mainframe
  python3 empstat.py nuevas --dias 7           (ofertas que coinciden y aparecieron en los ultimos 7 dias)
  python3 empstat.py historia                  (cifras de ejecuciones anteriores)
  python3 empstat.py descargar                 (solo baja el CSV)

La salida normal va a stdout; solo los errores van a stderr.
"""
import argparse, csv, io, os, re, sqlite3, sys, textwrap, time
from urllib import request, error

VERSION = "1.0.0"
URL_CSV = "https://storage.googleapis.com/jobboard-free/all_jobs.csv"
DIR = os.path.join(os.path.expanduser("~"), "empstat")
ANCHO = 121

# nombre -> expresion regular (sobre el TITULO, sin distinguir mayusculas)
PATRONES = {
    "vb6": r"\bvb\s?6\b|\bvb\s?classic\b|visual\s*basic\s*6",
    "visual basic": r"visual\s*basic|\bvb\b(?!\s*\.?net)",
    "vb.net": r"\bvb\.?net\b|visual\s*basic\s*\.?net",
    "altsystem": r"alt-?system|\blegacy\b|bestehende?n?\s+system|altanwendung|modernisierung\s+(?:von|der)\s+(?:it|system|anwend)",
    "migration": r"\bmigration\b|\bmigrat(?:ion|e)\b.*\b(?:system|cloud|data|daten)",
    "as/400": r"\bas/?400\b|\biseries\b|\bibm\s?i\b|\brpg\b",
    "cobol": r"\bcobol\b|\bmainframe\b|\bjcl\b",
    "delphi": r"\bdelphi\b|\bpascal\b",
    "powerbuilder": r"power\s?builder",
    "ms access": r"\bms[- ]?access\b|\baccess\s+(?:datenbank|database|entwickl)",
    "vba": r"\bvba\b|\bexcel\s+makro",
    "classic asp": r"classic\s+asp|\basp\s+classic\b|\bvbscript\b",
    "foxpro": r"fox\s?pro",
    "abap": r"\babap\b|\bsap\s+(?:basis|abap)",
    "perl": r"\bperl\b",
    "fortran": r"\bfortran\b",
}


def out(s=""):
    print(s, flush=True)


def err(s):
    print(s, file=sys.stderr, flush=True)


def ahora():
    return time.strftime("%Y-%m-%d-%H-%M-%S")


def descarga(ruta_csv, tiempo=120):
    os.makedirs(DIR, exist_ok=True)
    out(f"Descargando {URL_CSV} ...")
    try:
        rq = request.Request(URL_CSV, headers={"User-Agent": "empstat/" + VERSION})
        with request.urlopen(rq, timeout=tiempo) as r:
            datos = r.read()
    except (error.URLError, OSError) as e:
        raise RuntimeError(f"No se pudo descargar el CSV ({e}). Comprueba la conexion o prueba mas tarde.")
    if len(datos) < 1000 or b"company" not in datos[:400]:
        raise RuntimeError(f"El archivo descargado no parece el CSV esperado ({len(datos)} bytes, empieza por "
                           f"{datos[:60]!r}).")
    with open(ruta_csv, "wb") as f:
        f.write(datos)
    out(f"CSV guardado: {ruta_csv} ({len(datos) // 1024} KB)")
    return datos


def lee_csv(datos):
    txt = datos.decode("utf-8-sig", errors="replace")
    filas = list(csv.DictReader(io.StringIO(txt)))
    if not filas or "title" not in filas[0]:
        raise RuntimeError(f"El CSV no tiene la columna 'title'. Columnas: {list(filas[0].keys()) if filas else '-'}")
    return filas


def abre_db():
    os.makedirs(DIR, exist_ok=True)
    db = sqlite3.connect(os.path.join(DIR, "empstat.db"))
    db.execute("CREATE TABLE IF NOT EXISTS historia (fecha TEXT, nombre TEXT, n INTEGER, total INTEGER, empresas INTEGER)")
    db.execute("""CREATE TABLE IF NOT EXISTS vistas (jobid TEXT, nombre TEXT, primera TEXT, ultima TEXT, empresa TEXT,
                  titulo TEXT, ciudad TEXT, url TEXT, PRIMARY KEY (jobid, nombre))""")
    return db


def obtiene(a):
    ruta = os.path.join(DIR, "all_jobs.csv")
    if a.local:
        if not os.path.exists(ruta):
            raise RuntimeError("No hay CSV local; ejecuta primero 'descargar' o quita --local.")
        with open(ruta, "rb") as f:
            datos = f.read()
        out(f"Usando el CSV local ({time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(ruta)))}).")
    else:
        datos = descarga(ruta)
    return lee_csv(datos)


def patrones_de(a):
    if a.regex:
        return {a.nombre or a.regex[:20]: re.compile(a.regex, re.I)}
    nombres = [x.strip() for x in a.lista.split(",")] if a.lista else list(PATRONES)
    res = {}
    for n in nombres:
        p = PATRONES.get(n.lower())
        if p is None:
            p = re.escape(n)       # palabra suelta no prevista: se busca tal cual
        res[n] = re.compile(p, re.I)
    return res


def cmd_contar(a):
    filas = obtiene(a)
    pats = patrones_de(a)
    db = abre_db()
    fecha = time.strftime("%Y-%m-%d %H:%M")
    total = len(filas)
    empresas = len({f.get("company", "") for f in filas})
    out(f"{total} ofertas de {empresas} empresas en el CSV.")
    L = [f"EMPSTAT - ofertas de Suiza por tecnologia   {fecha}   (CSV: {total} ofertas, {empresas} empresas; solo titulos)",
         "=" * ANCHO, f"{'Tecnologia':<16} {'Ofertas':>8} {'% del total':>12} {'Empresas':>9}  Empresas con mas ofertas", "-" * ANCHO]
    detalle = []
    for n, rx in pats.items():
        hit = [f for f in filas if rx.search(f.get("title", ""))]
        emp = {}
        for f in hit:
            emp[f.get("company", "?")] = emp.get(f.get("company", "?"), 0) + 1
        top = ", ".join(f"{c} ({k})" for c, k in sorted(emp.items(), key=lambda x: -x[1])[:3])
        linea = f"{n:<16} {len(hit):>8} {100 * len(hit) / max(total, 1):>11.2f}% {len(emp):>9}  {top}"
        L.append(linea if len(linea) <= ANCHO else linea[:ANCHO - 3] + "...")
        db.execute("INSERT INTO historia VALUES (?,?,?,?,?)", (fecha, n, len(hit), total, len(emp)))
        for f in hit:
            jid = f.get("jobId") or f.get("url", "")
            r = db.execute("SELECT 1 FROM vistas WHERE jobid=? AND nombre=?", (jid, n)).fetchone()
            if r:
                db.execute("UPDATE vistas SET ultima=? WHERE jobid=? AND nombre=?", (fecha, jid, n))
            else:
                db.execute("INSERT INTO vistas VALUES (?,?,?,?,?,?,?,?)", (jid, n, fecha, fecha, f.get("company", ""),
                           f.get("title", ""), f.get("city", ""), f.get("url", "")))
        detalle.append((n, hit))
    db.commit()
    L += ["-" * ANCHO]
    for t in ("Una oferta puede contar en varias tecnologias. 'altsystem' busca legacy/Altsystem/bestehende Systeme; "
              "'migration' es mas generico y sobreestima (incluye p. ej. oficinas cantonales de migracion).",
              "Se busca solo en el TITULO; el CSV no trae descripcion."):
        L += textwrap.wrap(t, ANCHO)
    if a.listar:
        for n, hit in detalle:
            if not hit:
                continue
            L += ["", f"{n.upper()} ({len(hit)})", "-" * 40]
            for f in hit:
                s = f"  {f.get('company', '?')[:22]:<22} {f.get('city', '')[:14]:<14} {f.get('title', '')}"
                L += textwrap.wrap(s, ANCHO, subsequent_indent=" " * 40) or [s]
                if f.get("url"):
                    L.append("    " + f["url"][:ANCHO - 4])
    ruta = a.salida or os.path.join(DIR, f"{ahora()}empstat.txt")
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    out("")
    for l in L:
        out(l)
    out(f"\nArchivo: {ruta}")
    out(f"Base local: {os.path.join(DIR, 'empstat.db')}")
    return 0


def cmd_nuevas(a):
    db = abre_db()
    desde = time.strftime("%Y-%m-%d", time.localtime(time.time() - a.dias * 86400))
    f = db.execute("SELECT nombre, primera, empresa, ciudad, titulo, url FROM vistas WHERE primera >= ? "
                   "ORDER BY primera DESC, nombre", (desde,)).fetchall()
    if not f:
        out(f"Sin ofertas nuevas desde {desde} (ejecuta 'contar' cada dia para ir acumulando).")
        return 0
    for n, p, e, c, t, u in f:
        s = f"{p[:10]} {n:<12} {e[:20]:<20} {c[:12]:<12} {t}"
        out(s if len(s) <= ANCHO else s[:ANCHO - 3] + "...")
    out(f"\n{len(f)} oferta(s) nueva(s) desde {desde}. La primera ejecucion marca todo como nuevo.")
    return 0


def cmd_historia(a):
    db = abre_db()
    f = db.execute("SELECT fecha, nombre, n, total, empresas FROM historia ORDER BY nombre, fecha").fetchall()
    if not f:
        out("Todavia no hay historia: ejecuta 'contar' primero.")
        return 0
    out(f"{'Fecha':<17} {'Tecnologia':<16} {'Ofertas':>8} {'Total CSV':>10} {'Empresas':>9}")
    for fe, n, k, t, e in f:
        out(f"{fe:<17} {n:<16} {k:>8} {t:>10} {e:>9}")
    return 0


def cmd_descargar(a):
    filas = lee_csv(descarga(os.path.join(DIR, "all_jobs.csv")))
    out(f"{len(filas)} ofertas, columnas: {', '.join(filas[0].keys())}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="empstat.py", description="empstat.py - estadisticas de ofertas de Suiza (CSV publico).")
    p.add_argument("--version", action="version", version=f"empstat {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("contar", help="Contar ofertas por tecnologia")
    c.add_argument("--lista", help="Nombres separados por comas (por defecto todos los conocidos)")
    c.add_argument("--regex", help="Expresion regular propia (se aplica al titulo)")
    c.add_argument("--nombre", help="Nombre para --regex")
    c.add_argument("--listar", action="store_true", help="Incluir en el .txt cada oferta coincidente")
    c.add_argument("--local", action="store_true", help="Usar el CSV ya descargado")
    c.add_argument("-o", "--salida")
    c.set_defaults(func=cmd_contar)
    n = sub.add_parser("nuevas", help="Ofertas coincidentes vistas por primera vez en los ultimos dias")
    n.add_argument("--dias", type=int, default=7)
    n.set_defaults(func=cmd_nuevas)
    h = sub.add_parser("historia", help="Cifras de ejecuciones anteriores")
    h.set_defaults(func=cmd_historia)
    d = sub.add_parser("descargar", help="Solo descargar el CSV")
    d.set_defaults(func=cmd_descargar)
    a = p.parse_args(argv)
    try:
        return a.func(a) or 0
    except RuntimeError as e:
        err(f"Error: {e}")
        return 1
    except KeyboardInterrupt:
        err("Interrumpido.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
