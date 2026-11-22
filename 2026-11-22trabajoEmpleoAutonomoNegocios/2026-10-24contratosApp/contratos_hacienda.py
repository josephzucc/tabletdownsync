#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
contratos_hacienda.py  -  Buscador de CONTRATOS (menores y licitaciones) con los
datos abiertos de la Plataforma de Contratacion del Sector Publico / Hacienda.

Lee los zip mensuales de Hacienda (ficheros .atom):
   - contratosMenoresPerfilesContratantes_AAAAMM.zip      (contratos MENORES)
   - licitacionesPerfilesContratanteCompleto3_AAAAMM.zip  (licitaciones NO menores)
los vuelca en una base SQLite local y permite buscar con muchos filtros.
Solo usa la biblioteca estandar de Python (vale para Termux sin instalar nada).

USO RAPIDO
  python contratos_hacienda.py buscar-meses --meses 202601-202602 --municipio "La Adrada"
        (por cada mes: descarga, busca, guarda resultados y BORRA el zip)
  python contratos_hacienda.py descargar 202601 --menores --licitaciones
  python contratos_hacienda.py indexar contratosMenoresPerfilesContratantes_202601.zip
  python contratos_hacienda.py                     (menu interactivo)
  python contratos_hacienda.py buscar --municipio "La Adrada"
  python contratos_hacienda.py buscar --provincia avila --fuente licitacion
  python contratos_hacienda.py buscar --provincia avila --texto ordenador --min 500
  python contratos_hacienda.py stats
  python contratos_hacienda.py ayuda
"""
import os, sys, re, sqlite3, zipfile, argparse, unicodedata, csv, io, time
import xml.etree.ElementTree as ET

AQUI = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(AQUI, "contratos_hacienda.db")
DESCARGAS = os.path.join(AQUI, "descargas_hacienda")
SALIDA = os.path.join(AQUI, "salida_contratos")

TIPO_CONTRATO = {"1": "Suministros", "2": "Servicios", "3": "Obras",
                 "21": "Gestion de servicios publicos", "22": "Concesion de servicios",
                 "31": "Concesion de obras", "32": "Concesion de obras publicas",
                 "40": "Colaboracion publico-privada", "7": "Administrativo especial",
                 "8": "Privado", "50": "Patrimonial"}
ESTADO = {"PUB": "Publicada", "EV": "En evaluacion", "ADJ": "Adjudicada",
          "RES": "Resuelta", "ANUL": "Anulada", "PRE": "Anuncio previo"}


def norm(s):
    """minusculas sin tildes para comparar."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower().strip()


def loc(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


# ----------------------------------------------------------------- indexado
def hijos(el, nombre):
    return [h for h in el if loc(h.tag) == nombre]


def hijo(el, nombre):
    for h in el:
        if loc(h.tag) == nombre:
            return h
    return None


def txt(el, *ruta):
    """texto del descendiente siguiendo la ruta de nombres locales."""
    for r in ruta:
        if el is None:
            return ""
        el = hijo(el, r)
    return (el.text or "").strip() if el is not None else ""


def parse_entry(e, fuente=""):
    d = {"fuente": fuente}
    d["id"] = txt(e, "id")
    d["actualizado"] = txt(e, "updated")
    d["titulo"] = txt(e, "title")
    ln = hijo(e, "link")
    d["enlace"] = ln.get("href") if ln is not None else ""
    cfs = hijo(e, "ContractFolderStatus")
    if cfs is None:
        return None
    d["expediente"] = txt(cfs, "ContractFolderID")
    d["estado"] = txt(cfs, "ContractFolderStatusCode")
    lcp = hijo(cfs, "LocatedContractingParty")
    org = nif = dir3 = ciudad = cp = email = tel = ""
    jer = []
    if lcp is not None:
        party = hijo(lcp, "Party")
        if party is not None:
            for pi in hijos(party, "PartyIdentification"):
                i = hijo(pi, "ID")
                if i is None:
                    continue
                if i.get("schemeName") == "NIF":
                    nif = (i.text or "").strip()
                elif i.get("schemeName") == "DIR3":
                    dir3 = (i.text or "").strip()
            org = txt(party, "PartyName", "Name")
            ciudad = txt(party, "PostalAddress", "CityName")
            cp = txt(party, "PostalAddress", "PostalZone")
            email = txt(party, "Contact", "ElectronicMail")
            tel = txt(party, "Contact", "Telephone")
        p = hijo(lcp, "ParentLocatedParty")
        while p is not None:
            n = txt(p, "PartyName", "Name")
            if n:
                jer.append(n)
            p = hijo(p, "ParentLocatedParty")
        d["perfil"] = txt(lcp, "BuyerProfileURIID")
    else:
        d["perfil"] = ""
    d.update(organo=org, nif_organo=nif, dir3=dir3, ciudad=ciudad, cp=cp,
             email_organo=email, tel_organo=tel, jerarquia=" > ".join(jer))
    pp = hijo(cfs, "ProcurementProject")
    d["tipo"] = d["cpv"] = d["lugar"] = ""
    d["importe_siva"] = d["importe_total"] = None
    d["duracion"] = ""
    if pp is not None:
        d["objeto"] = txt(pp, "Name")
        d["tipo"] = txt(pp, "TypeCode")
        d["importe_total"] = _f(txt(pp, "BudgetAmount", "TotalAmount"))
        d["importe_siva"] = _f(txt(pp, "BudgetAmount", "TaxExclusiveAmount"))
        cpvs = []
        for rc in hijos(pp, "RequiredCommodityClassification"):
            c = txt(rc, "ItemClassificationCode")
            if c:
                cpvs.append(c)
        d["cpv"] = ",".join(cpvs)
        d["lugar"] = txt(pp, "RealizedLocation", "CountrySubentity")
        dm = txt(pp, "PlannedPeriod", "DurationMeasure")
        dmx = hijo(hijo(pp, "PlannedPeriod"), "DurationMeasure") if hijo(pp, "PlannedPeriod") is not None else None
        d["duracion"] = (dm + " " + (dmx.get("unitCode") or "")).strip() if dmx is not None else ""
    else:
        d["objeto"] = d["titulo"]
    tr = hijo(cfs, "TenderResult")
    d["adjudicatario"] = d["nif_adjudicatario"] = d["fecha_adjudicacion"] = ""
    d["n_ofertas"] = None
    d["pyme"] = ""
    d["adj_siva"] = d["adj_total"] = None
    if tr is not None:
        d["fecha_adjudicacion"] = txt(tr, "AwardDate")
        d["n_ofertas"] = _i(txt(tr, "ReceivedTenderQuantity"))
        pyme = txt(tr, "SMEAwardedIndicator")
        d["pyme"] = {"true": "Si", "false": "No"}.get(pyme, pyme)
        wp = hijo(tr, "WinningParty")
        if wp is not None:
            d["adjudicatario"] = re.sub(r"\s+", " ", txt(wp, "PartyName", "Name"))
            d["nif_adjudicatario"] = txt(wp, "PartyIdentification", "ID")
        lm = hijo(hijo(tr, "AwardedTenderedProject"), "LegalMonetaryTotal") \
            if hijo(tr, "AwardedTenderedProject") is not None else None
        if lm is not None:
            d["adj_siva"] = _f(txt(lm, "TaxExclusiveAmount"))
            d["adj_total"] = _f(txt(lm, "PayableAmount"))
    tp = hijo(cfs, "TenderingProcess")
    d["procedimiento"] = txt(tp, "ProcedureCode") if tp is not None else ""
    # licitaciones (no menores): plazo de presentacion, valor estimado, lotes
    d["fecha_limite"] = ""
    if tp is not None:
        dl = hijo(tp, "TenderSubmissionDeadlinePeriod")
        if dl is not None:
            d["fecha_limite"] = (txt(dl, "EndDate") + " " + txt(dl, "EndTime")).strip()
    d["valor_estimado"] = _f(txt(pp, "BudgetAmount", "EstimatedOverallContractAmount")) \
        if pp is not None else None
    d["n_lotes"] = len(hijos(cfs, "ProcurementProjectLot"))
    # varias adjudicaciones (lotes): se concatenan adjudicatarios distintos
    trs = hijos(cfs, "TenderResult")
    if len(trs) > 1:
        nombres = []
        for t in trs:
            wp = hijo(t, "WinningParty")
            n = re.sub(r"\s+", " ", txt(wp, "PartyName", "Name")) if wp is not None else ""
            if n and n not in nombres:
                nombres.append(n)
        if nombres:
            d["adjudicatario"] = " / ".join(nombres)
    return d


def fuente_de(nombre):
    n = nombre.lower()
    if "menores" in n:
        return "menor"
    if "licitaciones" in n or "completo" in n:
        return "licitacion"
    return "otro"


def _f(s):
    try:
        return float(s)
    except Exception:
        return None


def _i(s):
    try:
        return int(s)
    except Exception:
        return None


COLS = ["id", "actualizado", "titulo", "enlace", "expediente", "estado", "organo",
        "nif_organo", "dir3", "ciudad", "cp", "email_organo", "tel_organo",
        "jerarquia", "perfil", "objeto", "tipo", "cpv", "lugar", "importe_siva",
        "importe_total", "duracion", "adjudicatario", "nif_adjudicatario",
        "fecha_adjudicacion", "n_ofertas", "pyme", "adj_siva", "adj_total",
        "procedimiento", "fuente", "valor_estimado", "fecha_limite", "n_lotes"]


def crear_db(con):
    con.execute("CREATE TABLE IF NOT EXISTS contratos (%s, PRIMARY KEY(id))" %
                ", ".join(COLS))
    con.execute("CREATE TABLE IF NOT EXISTS busq (id PRIMARY KEY, t, o, g)")
    for c in ("cp", "organo", "fecha_adjudicacion", "nif_adjudicatario"):
        con.execute("CREATE INDEX IF NOT EXISTS i_%s ON contratos(%s)" % (c, c))



# ------------------------------------------- lectura tolerante de ficheros ATOM
CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
NS_CONOCIDOS = {
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cbc-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
    "cac-place-ext": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
    "at": "http://purl.org/atompub/tombstones/1.0",
    "ns7": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
}
LOG_ERRORES = os.path.join(AQUI, "errores_indexado.log")


def decodificar(datos):
    """bytes -> (texto, codificacion). Prueba BOM, declaracion xml, utf-8, cp1252."""
    if datos.startswith(b"\xef\xbb\xbf"):
        return datos[3:].decode("utf-8", "replace"), "utf-8 (con BOM)"
    cand = []
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", datos[:300])
    if m:
        cand.append(m.group(1).decode("ascii", "replace"))
    cand += ["utf-8", "cp1252"]
    for c in cand:
        try:
            return datos.decode(c), c
        except Exception:
            continue
    return datos.decode("utf-8", "replace"), "utf-8 (con sustituciones)"


def cabecera_ns(texto):
    decl = dict(re.findall(r"xmlns:([\w.-]+)=\"([^\"]*)\"", texto[:30000]))
    for k, v in NS_CONOCIDOS.items():
        decl.setdefault(k, v)
    return '<feed xmlns="http://www.w3.org/2005/Atom" ' + " ".join(
        'xmlns:%s="%s"' % (k, v) for k, v in decl.items()) + ">"


def arreglar_fragmento(f):
    f = CTRL.sub("", f)
    f = AMP.sub("&amp;", f)
    if "</link>" not in f:     # <link ...> sin cerrar (estilo HTML)
        f = re.sub(r"<link\b([^>]*?)(?<!/)>", r"<link\1/>", f)
    return f


def entradas_tolerantes(datos, info):
    """Parte el fichero en <entry>...</entry> y parsea cada una por separado.
    info: dict donde se anotan estadisticas y errores."""
    texto, cod = decodificar(datos)
    info["codificacion"] = cod
    cab = cabecera_ns(texto)
    fallos = info.setdefault("fallos", [])
    info["encontradas"] = 0
    info["recuperadas"] = 0
    for m in re.finditer(r"<entry\b.*?</entry>", texto, re.S):
        info["encontradas"] += 1
        frag = m.group(0)
        ultimo = None
        for intento in (frag, arreglar_fragmento(frag)):
            try:
                raiz = ET.fromstring(cab + intento + "</feed>")
                info["recuperadas"] += 1
                yield raiz[0]
                break
            except ET.ParseError as ex:
                ultimo = ex
        else:
            if len(fallos) < 20:
                fallos.append((str(ultimo), frag[:700]))
            info["n_fallos"] = info.get("n_fallos", 0) + 1


def entradas_atom(abrir, nombre, info):
    """Generador de elementos <entry>. Intenta el modo rapido (streaming); si el
    XML esta mal formado, pasa al modo tolerante entrada por entrada."""
    info["modo"] = "normal"
    try:
        with abrir() as fh:
            for ev, el in ET.iterparse(fh, events=("end",)):
                if loc(el.tag) == "entry":
                    yield el
        return
    except ET.ParseError as ex:
        info["modo"] = "tolerante"
        info["error_inicial"] = str(ex)
    with abrir() as fh:
        datos = fh.read()
    yield from entradas_tolerantes(datos, info)


def anotar_log(nombre, info):
    try:
        with open(LOG_ERRORES, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n  XML mal formado: %s\n  codificacion: %s | entradas encontradas: %s | recuperadas: %s | perdidas: %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), nombre, info.get("error_inicial"),
                info.get("codificacion"), info.get("encontradas"), info.get("recuperadas"),
                info.get("n_fallos", 0)))
            for msg, frag in info.get("fallos", [])[:3]:
                f.write("  entrada perdida (%s): %s\n" % (msg, re.sub(r"\s+", " ", frag)[:300]))
            f.write("\n")
    except Exception:
        pass



# ------------------------------------------------------------- diagnostico
def _fuentes_origen(origen):
    """Lista (nombre, funcion que devuelve los bytes) de ficheros de un zip/carpeta/fichero."""
    out = []
    if os.path.isdir(origen):
        for f in sorted(os.listdir(origen)):
            p_ = os.path.join(origen, f)
            if os.path.isfile(p_):
                out.append((f, (lambda p_=p_: open(p_, "rb").read())))
    elif zipfile.is_zipfile(origen):
        z = zipfile.ZipFile(origen)
        for i in z.infolist():
            if not i.is_dir():
                out.append((i.filename, (lambda n=i.filename, z=z: z.read(n))))
    else:
        out.append((os.path.basename(origen), (lambda: open(origen, "rb").read())))
    return out


def analizar_fichero(nombre, datos):
    r = {"nombre": nombre, "bytes": len(datos)}
    r["inicio"] = datos[:350].decode("utf-8", "replace")
    r["bom"] = datos.startswith(b"\xef\xbb\xbf")
    m = re.match(rb"\s*<\?xml[^>]*encoding=[\"']([^\"']+)", datos[:300])
    r["decl"] = m.group(1).decode("ascii", "replace") if m else "(sin declaracion)"
    try:
        datos.decode("utf-8")
        r["utf8"] = True
    except UnicodeDecodeError as ex:
        r["utf8"] = "NO (byte %d: %s)" % (ex.start, ex.reason)
    r["lineas"] = datos.count(b"\n") + 1
    r["n_abre"] = len(re.findall(rb"<entry\b", datos))
    r["n_cierra"] = datos.count(b"</entry>")
    r["parece_html"] = bool(re.search(rb"<html|<!doctype html", datos[:3000], re.I))
    r["ctrl"] = len(CTRL.findall(datos.decode("utf-8", "replace")))
    r["amp_sueltos"] = len(AMP.findall(datos.decode("utf-8", "replace")))
    mm = re.search(rb"<feed\b[^>]*>", datos[:20000])
    r["raiz"] = (mm.group(0)[:300].decode("utf-8", "replace") if mm else "(no se encuentra <feed>)")
    # --- estricto
    n = 0
    try:
        for ev, el in ET.iterparse(io.BytesIO(datos), events=("end",)):
            if loc(el.tag) == "entry":
                n += 1
                el.clear()
        if n == 0:
            r["estricto"] = "SIN ENTRADAS (XML valido pero vacio o no es un feed de contratos)"
            r["estricto_ok"] = False
        else:
            r["estricto"] = "OK (%d entradas)" % n
            r["estricto_ok"] = True
    except ET.ParseError as ex:
        r["estricto"] = "ERROR: %s" % ex
        r["estricto_ok"] = False
        try:
            ln, col = ex.position
            lineas = datos.decode("utf-8", "replace").split("\n")
            linea = lineas[ln - 1] if 0 < ln <= len(lineas) else ""
            ini = max(0, col - 250)
            r["contexto"] = (linea[ini:col] + "  >>>AQUI<<<  " + linea[col:col + 250])
            r["pos"] = (ln, col)
        except Exception:
            r["contexto"] = ""
    # --- tolerante
    info = {}
    campos = {}
    nt = 0
    for el in entradas_tolerantes(datos, info):
        d = parse_entry(el, "x")
        el.clear()
        nt += 1
        if nt <= 300 and d:
            for c in COLS:
                if d.get(c) not in (None, "", 0):
                    campos[c] = campos.get(c, 0) + 1
        elif nt == 1 and not d:
            r["entrada_vacia"] = True
    r["tol_enc"] = info.get("encontradas", 0)
    r["tol_rec"] = info.get("recuperadas", 0)
    r["tol_fallos"] = info.get("fallos", [])
    r["tol_n_fallos"] = info.get("n_fallos", 0)
    r["campos"] = campos
    r["muestra_n"] = min(nt, 300)
    return r


def diagnosticar(origen, informe=None, maximo=None):
    fuentes = _fuentes_origen(origen)
    if not fuentes:
        print("No hay ficheros en", origen)
        return
    if maximo:
        fuentes = fuentes[:maximo]
    informe = informe or os.path.join(AQUI, "diagnostico_%s.txt" % re.sub(r"\W+", "_", os.path.basename(origen.rstrip("/")))[:40])
    res = []
    t0 = time.time()
    for k, (nombre, leer) in enumerate(fuentes, 1):
        print("\r Analizando %d/%d  %s" % (k, len(fuentes), nombre[-45:]), end="", flush=True)
        try:
            res.append(analizar_fichero(nombre, leer()))
        except Exception as ex:
            res.append({"nombre": nombre, "bytes": 0, "excepcion": repr(ex)})
    print()
    L = []
    L.append("DIAGNOSTICO DE FICHEROS ATOM")
    L.append("=" * 78)
    L.append("Origen : %s" % origen)
    L.append("Fecha  : %s   Python %s" % (time.strftime("%Y-%m-%d %H:%M"), sys.version.split()[0]))
    L.append("Ficheros analizados: %d (%.1f s)" % (len(res), time.time() - t0))
    ok = [r for r in res if r.get("estricto_ok")]
    mal = [r for r in res if not r.get("estricto_ok")]
    L.append("XML correcto a la primera: %d   |   XML defectuoso o no valido: %d" % (len(ok), len(mal)))
    tipos = {}
    for r in mal:
        k = re.sub(r"[:,]? ?line \d+, column \d+", "", r.get("estricto", r.get("excepcion", "?")))[:70]
        tipos[k] = tipos.get(k, 0) + 1
    if tipos:
        L.append("\nTipos de error:")
        for k, v in sorted(tipos.items(), key=lambda x: -x[1]):
            L.append("  %4d  %s" % (v, k))
    tot_enc = sum(r.get("tol_enc", 0) for r in res)
    tot_rec = sum(r.get("tol_rec", 0) for r in res)
    L.append("\nModo tolerante (entrada por entrada): %d encontradas, %d recuperadas, %d perdidas" % (
        tot_enc, tot_rec, tot_enc - tot_rec))
    # campos vacios en general
    agreg = {}
    nmu = 0
    for r in res:
        for c, v in r.get("campos", {}).items():
            agreg[c] = agreg.get(c, 0) + v
        nmu += r.get("muestra_n", 0)
    if nmu:
        L.append("\nCampos rellenos en la muestra (%d entradas):" % nmu)
        vacios = [c for c in COLS if agreg.get(c, 0) == 0]
        for c in COLS:
            L.append("  %-20s %6.1f %%" % (c, 100.0 * agreg.get(c, 0) / nmu))
        if vacios:
            L.append("  >> Campos SIEMPRE vacios (posible cambio de estructura): " + ", ".join(vacios))
    L.append("\n" + "=" * 78)
    L.append("RESUMEN POR FICHERO")
    L.append("%-52s %10s  %-8s %s" % ("fichero", "bytes", "estricto", "entradas(tol.)"))
    for r in res:
        L.append("%-52s %10d  %-8s %s" % (r["nombre"][-52:], r["bytes"],
                 "OK" if r.get("estricto_ok") else "ERROR", "%s/%s" % (r.get("tol_rec", "-"), r.get("tol_enc", "-"))))
    L.append("\n" + "=" * 78)
    L.append("DETALLE DE LOS FICHEROS CON PROBLEMAS (maximo 8)")
    for r in mal[:8]:
        L.append("\n--- %s  (%d bytes)" % (r["nombre"], r["bytes"]))
        if "excepcion" in r:
            L.append("  Excepcion: " + r["excepcion"])
            continue
        L.append("  Empieza por       : %r" % r["inicio"])
        L.append("  Declaracion/BOM   : %s / BOM=%s   utf-8 valido: %s" % (r["decl"], r["bom"], r["utf8"]))
        L.append("  Lineas: %d   <entry abiertos/cerrados: %d/%d   parece HTML: %s" % (
            r["lineas"], r["n_abre"], r["n_cierra"], r["parece_html"]))
        L.append("  Caracteres de control: %d   '&' sin escapar: %d" % (r["ctrl"], r["amp_sueltos"]))
        L.append("  Etiqueta <feed>   : %s" % r["raiz"])
        L.append("  Lectura estricta  : %s" % r["estricto"])
        if r.get("contexto"):
            L.append("  Contexto del error: %s" % r["contexto"])
        L.append("  Modo tolerante    : %d/%d entradas recuperadas" % (r["tol_rec"], r["tol_enc"]))
        for msg, frag in r["tol_fallos"][:2]:
            L.append("    entrada perdida (%s): %s" % (msg, re.sub(r"\s+", " ", frag)[:500]))
    if not mal:
        L.append("(ninguno)")
    # estructura de una entrada
    L.append("\n" + "=" * 78)
    L.append("MUESTRA DE ESTRUCTURA (primera entrada recuperable)")
    for nombre, leer in fuentes[:3]:
        try:
            datos = leer()
            for el in entradas_tolerantes(datos, {}):
                rutas = []
                def rec(e, ruta):
                    h = list(e)
                    nm = ruta + "/" + loc(e.tag)
                    if not h:
                        rutas.append(nm)
                    for x in h:
                        rec(x, nm)
                rec(el, "")
                L.append("%s: %d rutas, p. ej.:" % (nombre, len(rutas)))
                vistos = []
                for rr in rutas:
                    rr2 = re.sub(r"/ParentLocatedParty(/ParentLocatedParty)+", "/ParentLocatedParty+", rr)
                    if rr2 not in vistos:
                        vistos.append(rr2)
                for rr in vistos[:70]:
                    L.append("   " + rr)
                break
            break
        except Exception:
            continue
    with open(informe, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L[:22 + len(tipos)]))
    print("\nInforme completo guardado en:\n  %s\nEnviame ese fichero y lo reparo." % informe)
    return informe

def indexar(origen, rehacer=False, db=None, silencioso=False):
    db = db or DB
    if rehacer and os.path.exists(db):
        os.remove(db)
    con = sqlite3.connect(db)
    crear_db(con)
    fuentes = []  # (nombre, abrir)
    if os.path.isdir(origen):
        for f in sorted(os.listdir(origen)):
            if f.endswith(".atom"):
                p = os.path.join(origen, f)
                fuentes.append((f, (lambda p=p: open(p, "rb"))))
    elif zipfile.is_zipfile(origen):
        z = zipfile.ZipFile(origen)
        for n in sorted(z.namelist()):
            if n.endswith(".atom"):
                fuentes.append((n, (lambda n=n, z=z: z.open(n))))
    else:
        fuentes.append((os.path.basename(origen), (lambda: open(origen, "rb"))))
    if not fuentes:
        print("No se han encontrado ficheros .atom en", origen)
        return
    total = 0
    ph = ",".join("?" * len(COLS))
    problemas = []
    for k, (nombre, abrir) in enumerate(fuentes, 1):
        n = 0
        info = {}
        try:
            for el in entradas_atom(abrir, nombre, info):
                d = parse_entry(el, fuente_de(nombre))
                el.clear()
                if not d or not d["id"]:
                    continue
                # si ya existe, solo reemplazar si es mas reciente
                r = con.execute("SELECT actualizado FROM contratos WHERE id=?",
                                (d["id"],)).fetchone()
                if r and r[0] and r[0] >= d["actualizado"]:
                    continue
                con.execute("INSERT OR REPLACE INTO contratos VALUES (%s)" % ph,
                            [d[c] for c in COLS])
                blob = norm(" | ".join(str(d[c] or "") for c in (
                    "objeto", "organo", "jerarquia", "ciudad", "adjudicatario",
                    "nif_adjudicatario", "expediente", "lugar", "cpv")))
                blo = norm(d["organo"])
                blg = norm(" | ".join(str(d[c] or "") for c in (
                    "organo", "jerarquia", "ciudad", "lugar")))
                con.execute("INSERT OR REPLACE INTO busq VALUES (?,?,?,?)",
                            (d["id"], blob, blo, blg))
                n += 1
        except Exception as ex:   # un fichero roto no debe tumbar todo el indexado
            info["error_fatal"] = str(ex)
        if info.get("modo") == "tolerante" or info.get("error_fatal"):
            anotar_log(nombre, info)
            txt_p = "%s: XML mal formado (%s); modo tolerante: %s de %s entradas recuperadas" % (
                nombre, (info.get("error_inicial") or info.get("error_fatal") or "")[:60],
                info.get("recuperadas", 0), info.get("encontradas", 0))
            problemas.append(txt_p)
            print("\n   [aviso] " + txt_p)
        elif info.get("modo") == "normal" and n == 0 and not silencioso:
            pass
        con.commit()
        total += n
        if not silencioso:
            print("[%d/%d] %s: %d contratos nuevos/actualizados" % (k, len(fuentes), nombre, n))
        else:
            print("\r   indexando %d/%d ficheros" % (k, len(fuentes)), end="", flush=True)
    con.close()
    if silencioso:
        print()
    else:
        print("Listo. Total tratados: %d. Base de datos: %s" % (total, db))
    if problemas:
        print("   %d fichero(s) con XML defectuoso (detalle en %s). Para un informe completo:\n"
              "   python contratos_hacienda.py diagnosticar %s" % (len(problemas), LOG_ERRORES, origen))
    return problemas


# ----------------------------------------------------------------- busqueda
def construir_consulta(a):
    w, p = [], []

    def like(col, val):
        for v in val if isinstance(val, list) else [val]:
            w.append("c.%s LIKE ? ESCAPE '\\'" % col)
            p.append("%" + esc(v) + "%")

    # normalizamos con la columna busq (sin tildes) para texto libre
    j = "JOIN busq b ON b.id=c.id"
    if a.get("texto"):
        for t in a["texto"]:
            w.append("b.t LIKE ? ESCAPE '\\'")
            p.append("%" + esc(norm(t)) + "%")
    if a.get("organo"):
        w.append("b.o LIKE ? ESCAPE '\\'")
        p.append("%" + esc(norm(a["organo"])) + "%")
    if a.get("municipio"):
        # organo (Ayuntamiento de X), ciudad o jerarquia
        w.append("b.g LIKE ? ESCAPE '\\'")
        p.append("%" + esc(norm(a["municipio"])) + "%")
    if a.get("provincia"):
        prov = norm(a["provincia"])
        cp = PROV_CP.get(prov)
        cond = "b.g LIKE ? ESCAPE '\\'"
        p.append("%" + esc(prov) + "%")
        if cp:
            cond = "(%s OR c.cp LIKE ?)" % cond
            p.append(cp + "%")
        w.append(cond)
    if a.get("ccaa"):
        w.append("b.g LIKE ? ESCAPE '\\'")
        p.append("%" + esc(norm(a["ccaa"])) + "%")
    if a.get("adjudicatario"):
        w.append("c.adjudicatario LIKE ? ESCAPE '\\'")
        p.append("%" + esc(a["adjudicatario"]) + "%")
    if a.get("nif"):
        w.append("(UPPER(c.nif_adjudicatario)=? OR UPPER(c.nif_organo)=?)")
        p += [a["nif"].upper()] * 2
    if a.get("cpv"):
        w.append("c.cpv LIKE ?")
        p.append(a["cpv"] + "%" if not a["cpv"].startswith("%") else a["cpv"])
    if a.get("tipo"):
        t = {"suministros": "1", "servicios": "2", "obras": "3"}.get(norm(a["tipo"]), a["tipo"])
        w.append("c.tipo=?")
        p.append(t)
    if a.get("estado"):
        w.append("UPPER(c.estado)=?")
        p.append(a["estado"].upper())
    if a.get("min") is not None:
        w.append("COALESCE(c.adj_siva, c.importe_siva) >= ?")
        p.append(a["min"])
    if a.get("max") is not None:
        w.append("COALESCE(c.adj_siva, c.importe_siva) <= ?")
        p.append(a["max"])
    if a.get("desde"):
        w.append(FECHA_EF + " >= ?")
        p.append(a["desde"])
    if a.get("hasta"):
        w.append(FECHA_EF + " <= ?")
        p.append(a["hasta"])
    if a.get("fuente"):
        w.append("c.fuente=?")
        p.append({"menores": "menor", "menor": "menor", "licitaciones": "licitacion",
                  "licitacion": "licitacion"}.get(norm(a["fuente"]), a["fuente"]))
    if a.get("sin_adjudicar"):
        w.append("(c.adjudicatario IS NULL OR c.adjudicatario='')")
    orden = {"importe": "COALESCE(c.adj_siva,c.importe_siva) DESC",
             "fecha": FECHA_EF + " DESC",
             "organo": "c.organo, " + FECHA_EF + " DESC",
             "adjudicatario": "c.adjudicatario, " + FECHA_EF + " DESC"
             }.get(a.get("orden") or "fecha", FECHA_EF + " DESC")
    sql = "SELECT c.* FROM contratos c %s%s ORDER BY %s" % (
        j, (" WHERE " + " AND ".join(w)) if w else "", orden)
    if a.get("limite"):
        sql += " LIMIT %d" % int(a["limite"])
    return sql, p


FECHA_EF = "COALESCE(NULLIF(c.fecha_adjudicacion,''), SUBSTR(c.actualizado,1,10))"


def esc(s):
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


PROV_CP = {"alava": "01", "araba": "01", "albacete": "02", "alicante": "03",
           "almeria": "04", "avila": "05", "badajoz": "06", "baleares": "07",
           "illes balears": "07", "barcelona": "08", "burgos": "09",
           "caceres": "10", "cadiz": "11", "castellon": "12", "ciudad real": "13",
           "cordoba": "14", "a coruna": "15", "coruna": "15", "cuenca": "16",
           "girona": "17", "gerona": "17", "granada": "18", "guadalajara": "19",
           "gipuzkoa": "20", "guipuzcoa": "20", "huelva": "21", "huesca": "22",
           "jaen": "23", "leon": "24", "lleida": "25", "lerida": "25",
           "la rioja": "26", "lugo": "27", "madrid": "28", "malaga": "29",
           "murcia": "30", "navarra": "31", "ourense": "32", "asturias": "33",
           "palencia": "34", "las palmas": "35", "pontevedra": "36",
           "salamanca": "37", "santa cruz de tenerife": "38", "cantabria": "39",
           "segovia": "40", "sevilla": "41", "soria": "42", "tarragona": "43",
           "teruel": "44", "toledo": "45", "valencia": "46", "valladolid": "47",
           "bizkaia": "48", "vizcaya": "48", "zamora": "49", "zaragoza": "50",
           "ceuta": "51", "melilla": "52"}


def abrir_db():
    if not os.path.exists(DB):
        print("Aun no hay base de datos. Ejecuta primero:")
        print("   python menores_hacienda.py indexar <fichero.zip | carpeta con .atom>")
        sys.exit(1)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def eur(x):
    if x is None:
        return "-"
    return ("{:,.2f}".format(x)).replace(",", "X").replace(".", ",").replace("X", ".")


def lineas_ficha(r):
    imp = r["adj_siva"] if r["adj_siva"] is not None else r["importe_siva"]
    tot = r["adj_total"] if r["adj_total"] is not None else r["importe_total"]
    L = [
        ("Expediente", r["expediente"]),
        ("Objeto", r["objeto"]),
        ("Organo", r["organo"]),
        ("Jerarquia", r["jerarquia"]),
        ("Ciudad / CP", "%s / %s" % (r["ciudad"], r["cp"])),
        ("Fuente", {"menor": "Contrato menor", "licitacion": "Licitacion (no menor)"}.get(r["fuente"], r["fuente"])),
        ("Tipo contrato", TIPO_CONTRATO.get(r["tipo"], r["tipo"])),
        ("CPV", r["cpv"]),
        ("Importe sin IVA", eur(imp) + " EUR"),
        ("Importe con IVA", eur(tot) + " EUR"),
        ("Valor estimado", (eur(r["valor_estimado"]) + " EUR") if r["valor_estimado"] else ""),
        ("Fin presentacion", r["fecha_limite"]),
        ("Lotes", r["n_lotes"] if r["n_lotes"] else ""),
        ("Ult. actualizac.", (r["actualizado"] or "")[:10]),
        ("Adjudicatario", "%s (%s)" % (r["adjudicatario"], r["nif_adjudicatario"])
         if r["adjudicatario"] else "(sin adjudicar)"),
        ("F. adjudicacion", r["fecha_adjudicacion"]),
        ("Ofertas / PYME", "%s / %s" % (r["n_ofertas"] if r["n_ofertas"] is not None else "-", r["pyme"] or "-")),
        ("Duracion", r["duracion"]),
        ("Lugar ejecucion", r["lugar"]),
        ("Estado", ESTADO.get(r["estado"], r["estado"])),
        ("Contacto organo", "%s %s" % (r["email_organo"], r["tel_organo"])),
        ("Enlace", r["enlace"]),
    ]
    return [(k, v) for k, v in L if v and str(v).strip() not in ("-", "- EUR", "/", "- / -")]


def ficha(r, n=None):
    out = []
    cab = "=" * 78
    out.append(cab)
    if n is not None:
        out.append("# %d" % n)
    for k, v in lineas_ficha(r):
        out.append("%-17s: %s" % (k, v))
    return "\n".join(out)


def tabla(filas):
    """Tabla en texto plano alineada."""
    cab = ["#", "Fecha", "Organo", "Objeto", "Adjudicatario", "Importe s/IVA"]
    anch = [4, 10, 28, 40, 26, 13]
    def cortar(s, n):
        s = re.sub(r"\s+", " ", str(s or ""))
        return s if len(s) <= n else s[:n - 1] + "…"
    lin = []
    lin.append("  ".join(c.ljust(w) if i != 5 else c.rjust(w) for i, (c, w) in enumerate(zip(cab, anch))))
    lin.append("  ".join("-" * w for w in anch))
    for i, r in enumerate(filas, 1):
        imp = r["adj_siva"] if r["adj_siva"] is not None else r["importe_siva"]
        v = [str(i), r["fecha_adjudicacion"] or (r["actualizado"] or "")[:10], r["organo"], r["objeto"],
             r["adjudicatario"] or "(sin adjudicar)", eur(imp)]
        lin.append("  ".join(cortar(x, w).ljust(w) if j != 5 else cortar(x, w).rjust(w)
                             for j, (x, w) in enumerate(zip(v, anch))))
    return "\n".join(lin)


def resumen(filas):
    tot = sum((r["adj_siva"] if r["adj_siva"] is not None else (r["importe_siva"] or 0)) or 0 for r in filas)
    return "Resultados: %d   |   Suma importes sin IVA: %s EUR" % (len(filas), eur(tot))


def buscar(a, ver_fichas=False, exportar=None):
    con = abrir_db()
    sql, p = construir_consulta(a)
    filas = con.execute(sql, p).fetchall()
    print(resumen(filas))
    if not filas:
        return filas
    if ver_fichas:
        print("\n\n".join(ficha(r, i) for i, r in enumerate(filas, 1)))
    else:
        print(tabla(filas))
    if exportar:
        os.makedirs(SALIDA, exist_ok=True)
        ruta = exportar if os.path.dirname(exportar) else os.path.join(SALIDA, exportar)
        if ruta.endswith(".csv"):
            with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(COLS)
                for r in filas:
                    w.writerow([r[c] for c in COLS])
        else:
            with open(ruta, "w", encoding="utf-8") as f:
                f.write(resumen(filas) + "\n\n")
                f.write("\n\n".join(ficha(r, i) for i, r in enumerate(filas, 1)))
                f.write("\n")
        print("\nExportado a:", ruta)
    return filas


def stats():
    con = abrir_db()
    q = lambda s, *p: con.execute(s, p).fetchall()
    n = q("SELECT COUNT(*) FROM contratos")[0][0]
    print("Contratos en la base: %d" % n)
    r = q("SELECT MIN(fecha_adjudicacion), MAX(fecha_adjudicacion) FROM contratos")[0]
    print("Fechas de adjudicacion: %s  a  %s" % (r[0], r[1]))
    r = q("SELECT SUM(COALESCE(adj_siva,importe_siva)) FROM contratos")[0][0]
    print("Importe total sin IVA: %s EUR" % eur(r))
    print("\nTop 15 organos por numero de contratos")
    for o, c, s in q("SELECT organo, COUNT(*), SUM(COALESCE(adj_siva,importe_siva)) FROM contratos GROUP BY organo ORDER BY 2 DESC LIMIT 15"):
        print("  %6d  %16s  %s" % (c, eur(s), o))
    print("\nContratos por provincia (por codigo postal del organo)")
    inv = {}
    for k, v in PROV_CP.items():
        inv.setdefault(v, k.title())
    for cp, c in q("SELECT SUBSTR(cp,1,2), COUNT(*) FROM contratos GROUP BY 1 ORDER BY 2 DESC LIMIT 12"):
        print("  %6d  %s %s" % (c, cp, inv.get(cp, "")))


URLS_ZIP = {
    "menor": [
        "https://contrataciondelestado.es/sindicacion/sindicacion_1143/contratosMenoresPerfilesContratantes_%s.zip",
        "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_1143/contratosMenoresPerfilesContratantes_%s.zip"],
    "licitacion": [
        "https://contrataciondelsectorpublico.gob.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3_%s.zip",
        "https://contrataciondelestado.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3_%s.zip"],
}


def bajar_zip(clave, per):
    """Descarga el zip de un periodo (AAAAMM). Devuelve la ruta o None."""
    import urllib.request
    os.makedirs(DESCARGAS, exist_ok=True)
    for plantilla in URLS_ZIP[clave]:
        url = plantilla % per
        dest = os.path.join(DESCARGAS, os.path.basename(url))
        try:
            print("Descargando", url)
            rq = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(rq, timeout=120) as r, open(dest, "wb") as f:
                tot = 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    tot += len(b)
                    print("\r   %.1f MB" % (tot / 1e6), end="", flush=True)
            print()
            if not zipfile.is_zipfile(dest):
                raise RuntimeError("el fichero descargado no es un zip valido")
            with zipfile.ZipFile(dest) as z_:
                if not any(n_.endswith(".atom") for n_ in z_.namelist()):
                    raise RuntimeError("el zip no contiene ficheros .atom (periodo no disponible?)")
            return dest
        except Exception as ex:
            print("\n   fallo (%s)" % ex)
            if os.path.exists(dest):
                os.remove(dest)
    if len(per) == 6:
        print("   Pista: segun Hacienda los zips MENSUALES solo estan disponibles para los\n"
              "   ultimos meses; para anios anteriores usa el zip ANUAL (periodo de 4 cifras,\n"
              "   p. ej. 2018).")
    return None


def descargar(periodos, menores, licitaciones, y_indexar):
    bajados = []
    for per in periodos:
        for clave, activo in (("menor", menores), ("licitacion", licitaciones)):
            if not activo:
                continue
            d = bajar_zip(clave, per)
            if d:
                print("   guardado en", d)
                bajados.append(d)
            else:
                print("   No se pudo bajar %s %s. Descargalo a mano desde\n"
                      "   https://www.hacienda.gob.es/es-ES/GobiernoAbierto/Datos%%20Abiertos/Paginas/LicitacionesContratante.aspx" % (clave, per))
    if y_indexar:
        for z in bajados:
            indexar(z)
    return bajados


# ------------------------------------------- busqueda automatica por meses
RES_DB = os.path.join(AQUI, "resultados_busqueda.db")


def expandir_meses(lista):
    """['202601','202603-202605','2018','2019-2020'] ->
       ['202601','202603','202604','202605','2018','2019','2020']
       (un periodo de 4 cifras = zip ANUAL AAAA)"""
    out = []
    for m in lista:
        m = m.strip()
        mm = re.fullmatch(r"(\d{4,6})\s*[-:]\s*(\d{4,6})", m)
        if mm and len(mm.group(1)) == len(mm.group(2)):
            a, b = mm.group(1), mm.group(2)
            if len(a) == 4:
                out += [str(y) for y in range(int(a), int(b) + 1)]
            else:
                y, mo = int(a[:4]), int(a[4:6])
                yf, mf = int(b[:4]), int(b[4:6])
                while (y, mo) <= (yf, mf):
                    out.append("%04d%02d" % (y, mo))
                    mo += 1
                    if mo > 12:
                        y, mo = y + 1, 1
        elif re.fullmatch(r"\d{4}|\d{6}", m):
            out.append(m)
        else:
            print("Periodo no valido (usa AAAAMM, AAAA o rangos AAAAMM-AAAAMM):", m)
    vistos = []
    for m in out:
        if m not in vistos:
            vistos.append(m)
    return vistos


def buscar_meses(meses, fuentes, a, conservar_zip=False, acumular=False,
                 exportar=None, fichas=False):
    """Por cada mes: descarga -> indexa en BD temporal -> busca -> guarda los
    resultados en resultados_busqueda.db -> borra zip y BD temporal."""
    meses = expandir_meses(meses)
    if not meses:
        return
    a = dict(a)
    a.pop("limite", None)
    if not acumular and os.path.exists(RES_DB):
        os.remove(RES_DB)
    res = sqlite3.connect(RES_DB)
    crear_db(res)
    res.execute("CREATE TABLE IF NOT EXISTS hechos (mes, fuente, n, PRIMARY KEY(mes, fuente))")
    res.commit()
    fallos = []
    tmp = os.path.join(DESCARGAS, "_temporal.db")
    os.makedirs(DESCARGAS, exist_ok=True)
    total = len(meses) * len(fuentes)
    k = 0
    for mes in meses:
        for clave in fuentes:
            k += 1
            etiqueta = "%s %s" % ({"menor": "menores", "licitacion": "licitaciones"}[clave], mes)
            print("\n=== [%d/%d] %s ===" % (k, total, etiqueta))
            if acumular and res.execute("SELECT 1 FROM hechos WHERE mes=? AND fuente=?", (mes, clave)).fetchone():
                print("   ya procesado en una ejecucion anterior (--acumular): se omite")
                continue
            zpath = bajar_zip(clave, mes)
            if not zpath:
                fallos.append(etiqueta)
                continue
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
                indexar(zpath, db=tmp, silencioso=True)
                con = sqlite3.connect(tmp)
                con.row_factory = sqlite3.Row
                sql, p = construir_consulta(a)
                filas = con.execute(sql, p).fetchall()
                ids = [r["id"] for r in filas]
                ph = ",".join("?" * len(COLS))
                for r in filas:
                    res.execute("INSERT OR REPLACE INTO contratos VALUES (%s)" % ph, [r[c] for c in COLS])
                for i in range(0, len(ids), 500):
                    trozo = ids[i:i + 500]
                    for b in con.execute("SELECT * FROM busq WHERE id IN (%s)" % ",".join("?" * len(trozo)), trozo):
                        res.execute("INSERT OR REPLACE INTO busq VALUES (?,?,?,?)", tuple(b))
                res.execute("INSERT OR REPLACE INTO hechos VALUES (?,?,?)", (mes, clave, len(filas)))
                res.commit()
                con.close()
                print("   %d coincidencias en %s (guardadas)" % (len(filas), etiqueta))
            except Exception as ex:
                print("   ERROR procesando %s: %s" % (etiqueta, ex))
                fallos.append(etiqueta)
            finally:
                for f in (tmp, tmp + "-journal"):
                    if os.path.exists(f):
                        os.remove(f)
                if not conservar_zip and os.path.exists(zpath):
                    os.remove(zpath)
                    print("   zip eliminado")
    res.row_factory = sqlite3.Row
    todo = res.execute("SELECT * FROM contratos c ORDER BY " + FECHA_EF + " DESC").fetchall()
    print("\n" + "=" * 78)
    print("RESULTADO ACUMULADO (%d periodos)" % total)
    print(resumen(todo))
    if todo:
        print(tabla(todo) if not fichas else "\n\n".join(ficha(r, i) for i, r in enumerate(todo, 1)))
    if fallos:
        print("\nPeriodos que NO se pudieron procesar: " + ", ".join(fallos))
        print("Repite con --acumular y los mismos filtros para completar solo los que faltan.")
    if exportar and todo:
        _exp(todo, exportar)
    print("\nResultados guardados en: %s" % RES_DB)
    res.close()
    return todo


def ayuda():
    print(__doc__)
    print("""DIAGNOSTICO DE FICHEROS DEFECTUOSOS
  python contratos_hacienda.py diagnosticar fichero.zip [--informe informe.txt] [--max N]
  Analiza cada .atom (codificacion, XML mal formado y donde, entradas
  recuperables, campos vacios, estructura) y escribe un informe .txt para
  enviarlo y poder reparar el programa. El indexado ya usa un modo TOLERANTE
  automatico para ficheros con XML defectuoso (anota los casos en
  errores_indexado.log).
""")
    print("""BUSQUEDA AUTOMATICA POR MESES (no ocupa disco: borra cada zip al terminar)
  python contratos_hacienda.py buscar-meses --meses 202601 202602 [filtros]
  python contratos_hacienda.py buscar-meses --meses 202501-202512 --licitaciones --provincia avila
  --meses        meses AAAAMM, rangos AAAAMM-AAAAMM, o anios AAAA / AAAA-AAAA (zip anual)
  --menores / --licitaciones   que zips usar (por defecto menores; se pueden dar ambos)
  --conservar-zip   no borrar los zips
  --acumular        anade a los resultados de una ejecucion anterior y se salta
                    los meses ya hechos (util si se corto la conexion)
  --exportar nombre.txt|csv     --fichas
  Admite los mismos filtros que 'buscar'. Los resultados se acumulan en
  resultados_busqueda.db y se muestran juntos al final.
""")
    print("""FILTROS de 'buscar' (se pueden combinar; no distinguen mayusculas ni tildes):
  --texto PALABRA        busca en objeto, organo, adjudicatario, expediente, CPV...
                         (se puede repetir: todas las palabras deben aparecer)
  --organo "Ayuntamiento de X"
  --municipio "La Adrada"
  --provincia avila      (usa el nombre o el codigo postal 05xxx del organo)
  --ccaa "Castilla y Leon"
  --adjudicatario "nombre o parte"      --nif B12345678 (adjudicatario u organo)
  --cpv 72                (prefijo de codigo CPV; 72 = servicios informaticos)
  --tipo suministros|servicios|obras
  --fuente menores|licitaciones     (por defecto, ambas)
  --estado RES|ADJ|PUB...
  --min 500 --max 5000    (importe sin IVA, en euros)
  --desde 2026-01-01 --hasta 2026-01-31   (fecha de adjudicacion o, si no hay,
                         la de ultima actualizacion)
  --sin-adjudicar         contratos sin adjudicatario registrado
  --orden fecha|importe|organo|adjudicatario     --limite N
  --fichas                muestra ficha completa en vez de tabla
  --exportar nombre.txt|nombre.csv      (se guarda en la carpeta salida_menores)
""")


def preguntar(msg, defecto=""):
    try:
        r = input(msg).strip()
    except EOFError:
        r = ""
    return r or defecto


def menu():
    a = {}
    while True:
        print("\n" + "=" * 60)
        print(" CONTRATOS (menores y licitaciones) - Hacienda, datos abiertos")
        print("=" * 60)
        print(" 1) Buscar (asistente de filtros)")
        print(" 2) Atajo: Ayuntamiento de La Adrada")
        print(" 3) Atajo: provincia de Avila")
        print(" 4) Estadisticas de la base")
        print(" 5) Indexar un zip / carpeta de .atom")
        print(" 6) Ayuda")
        print(" 7) Descargar zips mensuales de Hacienda (menores y/o licitaciones)")
        print(" 8) BUSCAR POR MESES (descarga, busca y borra cada zip)")
        print(" 0) Salir")
        op = preguntar("Opcion: ")
        if op == "0":
            return
        if op == "1":
            a = {}
            t = preguntar("Palabras a buscar (vacio = todo): ")
            if t:
                a["texto"] = t.split()
            for k, msg in (("organo", "Organo contratante: "), ("municipio", "Municipio: "),
                           ("provincia", "Provincia: "), ("ccaa", "Comunidad autonoma: "),
                           ("adjudicatario", "Adjudicatario: "), ("nif", "NIF: "),
                           ("cpv", "CPV (prefijo): "), ("tipo", "Tipo (suministros/servicios/obras): "),
                           ("desde", "Adjudicado desde (AAAA-MM-DD): "),
                           ("hasta", "Adjudicado hasta (AAAA-MM-DD): ")):
                v = preguntar(msg)
                if v:
                    a[k] = v
            for k, msg in (("min", "Importe minimo s/IVA: "), ("max", "Importe maximo s/IVA: ")):
                v = preguntar(msg).replace(",", ".")
                if v:
                    try:
                        a[k] = float(v)
                    except ValueError:
                        pass
            a["orden"] = preguntar("Orden (fecha/importe/organo/adjudicatario) [fecha]: ", "fecha")
            a["limite"] = int(preguntar("Maximo de resultados [50]: ", "50") or 50)
        elif op == "2":
            a = {"municipio": "La Adrada", "orden": "fecha"}
            print("(busca en menores y licitaciones que tengas indexados)")
        elif op == "3":
            a = {"provincia": "avila", "orden": "fecha", "limite": 200}
        elif op == "4":
            stats(); continue
        elif op == "5":
            o = preguntar("Ruta del zip o carpeta: ")
            if o:
                indexar(o, rehacer=preguntar("Borrar base anterior? (s/N): ").lower() == "s")
            continue
        elif op == "6":
            ayuda(); continue
        elif op == "8":
            m = preguntar("Meses (AAAAMM o AAAAMM-AAAAMM, separados por espacio): ").split()
            q = preguntar("Que usar? m=menores l=licitaciones ml=ambos [m]: ", "m").lower()
            a2 = {}
            v = preguntar("Municipio (p. ej. La Adrada): ")
            if v:
                a2["municipio"] = v
            v = preguntar("Provincia: ")
            if v:
                a2["provincia"] = v
            v = preguntar("Palabras a buscar: ")
            if v:
                a2["texto"] = v.split()
            fu = (["menor"] if "m" in q else []) + (["licitacion"] if "l" in q else [])
            if m and fu:
                buscar_meses(m, fu, a2, False, preguntar("¿Acumular con resultados previos? (s/N): ").lower() == "s")
            continue
        elif op == "7":
            m = preguntar("Mes(es) AAAAMM separados por espacio (p.ej. 202601): ").split()
            q = preguntar("Que bajar? m=menores l=licitaciones ml=ambos [ml]: ", "ml").lower()
            if m:
                descargar(m, "m" in q, "l" in q, True)
            continue
        else:
            continue
        filas = buscar(a)
        while filas:
            x = preguntar("\nNumero para ver ficha, 'f' fichas de todos, 'e' exportar, Enter para volver: ")
            if not x:
                break
            if x.lower() == "f":
                print("\n\n".join(ficha(r, i) for i, r in enumerate(filas, 1)))
            elif x.lower() == "e":
                nom = preguntar("Nombre de fichero (.txt o .csv) [resultado.txt]: ", "resultado.txt")
                os.makedirs(SALIDA, exist_ok=True)
                buscar(a, ver_fichas=False, exportar=nom) if False else _exp(filas, nom)
            elif x.isdigit() and 1 <= int(x) <= len(filas):
                print(ficha(filas[int(x) - 1], int(x)))


def _exp(filas, nom):
    os.makedirs(SALIDA, exist_ok=True)
    ruta = os.path.join(SALIDA, nom)
    if nom.endswith(".csv"):
        with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(COLS)
            for r in filas:
                w.writerow([r[c] for c in COLS])
    else:
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(resumen(filas) + "\n\n")
            f.write("\n\n".join(ficha(r, i) for i, r in enumerate(filas, 1)) + "\n")
    print("Exportado a:", ruta)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("indexar")
    s.add_argument("origen")
    s.add_argument("--rehacer", action="store_true")
    b = sub.add_parser("buscar")
    b.add_argument("--texto", action="append")
    for k in ("organo", "municipio", "provincia", "ccaa", "adjudicatario", "nif",
              "cpv", "tipo", "estado", "desde", "hasta", "orden", "exportar", "fuente"):
        b.add_argument("--" + k)
    b.add_argument("--min", type=float)
    b.add_argument("--max", type=float)
    b.add_argument("--limite", type=int)
    b.add_argument("--sin-adjudicar", dest="sin_adjudicar", action="store_true")
    b.add_argument("--fichas", action="store_true")
    sub.add_parser("stats")
    sub.add_parser("ayuda")
    g_ = sub.add_parser("diagnosticar")
    g_.add_argument("origen", help="zip, carpeta o fichero .atom")
    g_.add_argument("--informe", help="fichero de salida (.txt)")
    g_.add_argument("--max", type=int, help="analizar solo los N primeros ficheros")
    m_ = sub.add_parser("buscar-meses")
    m_.add_argument("--meses", nargs="+", required=True,
                    help="AAAAMM sueltos o rangos AAAAMM-AAAAMM")
    m_.add_argument("--licitaciones", action="store_true", help="buscar en licitaciones (no menores)")
    m_.add_argument("--menores", action="store_true", help="buscar en menores (por defecto)")
    m_.add_argument("--conservar-zip", action="store_true")
    m_.add_argument("--acumular", action="store_true",
                    help="anadir a resultados anteriores y saltar los meses ya hechos")
    m_.add_argument("--fichas", action="store_true")
    m_.add_argument("--exportar")
    m_.add_argument("--texto", action="append")
    for k in ("organo", "municipio", "provincia", "ccaa", "adjudicatario", "nif",
              "cpv", "tipo", "estado", "desde", "hasta", "orden"):
        m_.add_argument("--" + k)
    m_.add_argument("--min", type=float)
    m_.add_argument("--max", type=float)
    m_.add_argument("--sin-adjudicar", dest="sin_adjudicar", action="store_true")
    d_ = sub.add_parser("descargar")
    d_.add_argument("meses", nargs="+", help="AAAAMM (p.ej. 202601) o AAAA (anual)")
    d_.add_argument("--menores", action="store_true")
    d_.add_argument("--licitaciones", action="store_true")
    d_.add_argument("--indexar", action="store_true", help="indexar tras descargar")
    ar = ap.parse_args()
    if ar.cmd == "indexar":
        indexar(ar.origen, ar.rehacer)
    elif ar.cmd == "buscar":
        a = {k: v for k, v in vars(ar).items() if v not in (None, False) and k not in ("cmd", "exportar", "fichas")}
        buscar(a, ar.fichas, ar.exportar)
    elif ar.cmd == "diagnosticar":
        diagnosticar(ar.origen, ar.informe, ar.max)
    elif ar.cmd == "buscar-meses":
        a = {k: v for k, v in vars(ar).items() if v not in (None, False)
             and k not in ("cmd", "meses", "licitaciones", "menores", "conservar_zip",
                           "acumular", "fichas", "exportar")}
        fu = (["menor"] if (ar.menores or not ar.licitaciones) else []) + (["licitacion"] if ar.licitaciones else [])
        buscar_meses(ar.meses, fu, a, ar.conservar_zip, ar.acumular, ar.exportar, ar.fichas)
    elif ar.cmd == "descargar":
        descargar(ar.meses, ar.menores or not ar.licitaciones, ar.licitaciones, ar.indexar)
    elif ar.cmd == "stats":
        stats()
    elif ar.cmd == "ayuda":
        ayuda()
    else:
        menu()


if __name__ == "__main__":
    main()
