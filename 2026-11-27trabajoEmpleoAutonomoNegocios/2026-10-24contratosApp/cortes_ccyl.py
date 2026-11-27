#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cortes_ccyl.py - Contratos menores de las CORTES DE CASTILLA Y LEON (ccyl.es).

OJO: esa web es del Parlamento autonomico (no de la Junta ni de ayuntamientos).
Publica un PDF por trimestre. Este programa:
   listar     -> muestra los PDF disponibles (de la web o de un HTML guardado)
   bajar      -> descarga los PDF (todos o los de un anio) y los convierte a texto
   buscar     -> busca palabras en los textos descargados (con lineas de contexto)

Requisito para convertir PDF a texto en Termux:   pkg install poppler
(si no esta, se intenta con la libreria pypdf: pip install pypdf)

Ejemplos
  python cortes_ccyl.py listar
  python cortes_ccyl.py listar --html contratosCyL-contratosMenores-lista.html
  python cortes_ccyl.py bajar --anio 2026
  python cortes_ccyl.py buscar informatica "seguridad" --contexto 1
"""
import os, re, sys, html, argparse, subprocess, urllib.request, urllib.parse, unicodedata

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(AQUI, "cortes_ccyl")
URL = "https://www.ccyl.es/Transparencia/ContratosMenores"


def norm(s):
    s = unicodedata.normalize("NFD", s)
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def leer(origen_html):
    if origen_html:
        return open(origen_html, encoding="utf-8", errors="replace").read()
    rq = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(rq, timeout=60).read().decode("utf-8", "replace")


def pdfs(origen_html=None):
    s = leer(origen_html)
    out = []
    for m in re.finditer(r'href=["\']?([^"\' >]+\.pdf)', s, re.I):
        h = html.unescape(m.group(1))
        if "contratosmenores" not in h.lower():
            continue
        fin = s.find("</a>", m.end())
        seg = s[m.end():fin] if fin > 0 else ""
        t = re.sub(r"\s+", " ", html.unescape(seg.rsplit(">", 1)[-1])).strip()
        out.append((urllib.parse.urljoin(URL, h), t))
    vistos, res = set(), []
    for h, t in out:
        if h not in vistos:
            vistos.add(h); res.append((h, t))
    return res


def a_texto(pdf, txt):
    try:
        subprocess.run(["pdftotext", "-layout", pdf, txt], check=True, capture_output=True)
        return True
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        with open(txt, "w", encoding="utf-8") as f:
            for p in PdfReader(pdf).pages:
                f.write((p.extract_text() or "") + "\n\f\n")
        return True
    except Exception as ex:
        print("   No puedo convertir a texto (instala poppler: pkg install poppler):", ex)
        return False


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    l = sub.add_parser("listar"); l.add_argument("--html")
    b = sub.add_parser("bajar"); b.add_argument("--html"); b.add_argument("--anio")
    s = sub.add_parser("buscar"); s.add_argument("palabras", nargs="+"); s.add_argument("--contexto", type=int, default=0)
    ar = ap.parse_args()
    if ar.cmd == "listar":
        for h, t in pdfs(ar.html):
            print("%-60s %s" % (t[:60], h))
    elif ar.cmd == "bajar":
        os.makedirs(DIR, exist_ok=True)
        for h, t in pdfs(ar.html):
            if ar.anio and not re.search(ar.anio, h + t):
                continue
            pdf = os.path.join(DIR, os.path.basename(h))
            print("Bajando", t, "...")
            try:
                rq = urllib.request.Request(h, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(rq, timeout=120) as r, open(pdf, "wb") as f:
                    f.write(r.read())
                if a_texto(pdf, pdf[:-4] + ".txt"):
                    print("   ok ->", pdf[:-4] + ".txt")
            except Exception as ex:
                print("   fallo:", ex)
    elif ar.cmd == "buscar":
        claves = [norm(p) for p in ar.palabras]
        n = 0
        for f in sorted(os.listdir(DIR)) if os.path.isdir(DIR) else []:
            if not f.endswith(".txt"):
                continue
            L = open(os.path.join(DIR, f), encoding="utf-8", errors="replace").read().split("\n")
            for i, l in enumerate(L):
                if all(k in norm(l) for k in claves):
                    n += 1
                    print("[%s] l.%d" % (f, i + 1))
                    for j in range(max(0, i - ar.contexto), min(len(L), i + ar.contexto + 1)):
                        print("   " + L[j].rstrip())
        print("\n%d coincidencias" % n)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
