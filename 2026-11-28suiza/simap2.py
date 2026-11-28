VERSION = "3.0.1"
import glob, hashlib, argparse, sqlite3, zipfile, email, email.policy, re, json, base64, unicodedata, csv, sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from email.utils import parsedate_to_datetime

import sys
import threading
from datetime import datetime


class TimestampedTee:
    def __init__(self, terminal, log_file, lock):
        self.terminal = terminal
        self.log_file = log_file
        self.lock = lock
        self.at_line_start = True

    def write(self, text):
        with self.lock:
            for char in text:
                if self.at_line_start:
                    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S ")
                    self.terminal.write(timestamp)
                    self.log_file.write(timestamp)
                    self.at_line_start = False

                self.terminal.write(char)
                self.log_file.write(char)

                if char == "\n":
                    self.at_line_start = True

            self.terminal.flush()
            self.log_file.flush()

        return len(text)

    def flush(self):
        with self.lock:
            self.terminal.flush()
            self.log_file.flush()


log_name = datetime.now().strftime("%Y-%m-%d-%H-%M-%S") + "simapenriquecer.txt"
log_file = open(log_name, "a", encoding="utf-8")
lock = threading.Lock()

sys.stdout = TimestampedTee(sys.__stdout__, log_file, lock)
sys.stderr = TimestampedTee(sys.__stderr__, log_file, lock)

print("This goes to the terminal and output.log")
print("Errors go there too", file=sys.stderr)


CANTONS='AG AI AR BE BL BS FR GE GL GR JU LU NE NW OW SG SH SO SZ TG TI UR VD VS ZG ZH'.split()
def norm(s): return ''.join(c for c in unicodedata.normalize('NFKD',s or '').casefold() if not unicodedata.combining(c))
def db(path):
 c=sqlite3.connect(path); c.row_factory=sqlite3.Row; c.create_function('norm',1,norm); c.execute('pragma foreign_keys=on'); c.executescript('''CREATE TABLE IF NOT EXISTS correos(id TEXT PRIMARY KEY, fecha TEXT, asunto TEXT, archivo TEXT, cuerpo TEXT);
 CREATE TABLE IF NOT EXISTS publicaciones(id TEXT PRIMARY KEY, proyecto TEXT, tipo TEXT, categoria TEXT, titulo TEXT, organismo TEXT, lugar_ejecucion TEXT, cp_postal TEXT, localidad_ejecucion TEXT, canton_ejecucion TEXT, municipio_organismo TEXT, canton_organismo TEXT, fundamento_origen TEXT, plazo TEXT, url TEXT, uuid_proyecto TEXT, uuid_lote TEXT, cpv TEXT, texto_original TEXT);
 CREATE TABLE IF NOT EXISTS fuentes(publicacion TEXT REFERENCES publicaciones(id), correo TEXT REFERENCES correos(id), PRIMARY KEY(publicacion,correo));
 CREATE INDEX IF NOT EXISTS idx_proyecto ON publicaciones(proyecto); CREATE INDEX IF NOT EXISTS idx_canton ON publicaciones(canton_ejecucion);'''); migrate(c); return c

def origin(org):
 # Only explicit names, not the place of performance.
 names={'zurich':'ZH','aargau':'AG','thurgau':'TG','luzern':'LU','bern':'BE','vaud':'VD','genève':'GE','geneve':'GE','schaffhausen':'SH','basel-landschaft':'BL','freiburg':'FR','neuchateloise':'NE'}
 o=norm(org); canton=None; municipio=None; why=None
 for name,code in names.items():
  if re.search(r'\b(?:kanton(?:spolizei)?|etat de|canton de|informatik|amt fur informatik)\s+'+re.escape(norm(name))+r'\b',o): canton=code; why='Nombre explícito del organismo'; break
 m=re.search(r'^(?:Einwohnergemeinde|Gemeindeverwaltung|Gemeinde|Stadt|Ville de)\s+([^,]+)',org or '',re.I)
 if m:
  municipio=re.split(r'\s+(?:Liegenschaften|–|-)\s*',m[1])[0].strip(); why='Nombre explícito del organismo'; canton=canton or {'luzern':'LU','biel':'BE','lausanne':'VD','interlaken':'BE','zumikon':'ZH','kirchdorf':None,'birsfelden':'BL'}.get(norm(municipio))
 return municipio,canton,why

def ingest(c, name, data):
 m=email.message_from_bytes(data,policy=email.policy.default); p=m.get_body(('plain',));
 if not p: raise ValueError('Sin cuerpo text/plain: '+name)
 s=p.get_content(); mid=str(m.get('Message-ID') or hashlib.sha256(data).hexdigest()); date=parsedate_to_datetime(str(m['Date'])).isoformat(); c.execute('INSERT OR IGNORE INTO correos VALUES(?,?,?,?,?)',(mid,date,str(m['Subject']),name,s))
 blocks=re.findall(r'<(https://www\.simap\.ch/[^>]+)>\s*\n\s*-\s*([^\n]+#\d+-\d+[^\n]*)\n(.*?)(?=\n\s*<https://|\n\[Ende Abschnitt|\Z)',s,re.S)
 expected=len(re.findall(r'^\s*- .*#\d+-\d+',s,re.M))
 if len(blocks)!=expected or not blocks: raise ValueError(f'{name}: {len(blocks)} bloques, {expected} esperados')
 for url,head,body in blocks:
  h=re.fullmatch(r'(.*?)\s+#(\d+-\d+)(?:\s*\|\s*(.*))?',head.strip()); tipo,id,cat=h.groups(); fields={}; title=[]
  for line in body.splitlines():
   line=line.strip()
   if line.startswith('>'): title.append(line[1:].strip())
   elif line.startswith('- ') and ':' in line: k,v=line[2:].split(':',1); fields[k]=v.strip()
  org=fields.get('Auftraggeber'); place=fields.get('Ort der Auftragserfüllung'); postal=local=canton=None
  loc=re.fullmatch(r'(\d{4}),\s*(.*),\s*([A-Z]{2})',place or '')
  if loc: postal,local,canton=loc.groups(); canton=canton if canton in CANTONS else None
  mun,co,why=origin(org); deadline=fields.get('Einreichefrist'); deadline=datetime.strptime(deadline,'%d.%m.%Y %H:%M').isoformat(' ') if deadline else None
  ctx=parse_qs(urlparse(url).query)['context'][0]; ctx=json.loads(base64.urlsafe_b64decode(ctx+'='*(-len(ctx)%4))); cpv='; '.join(re.findall(r'\b\d{8}(?:-\d)?\b',fields.get('CPV',''))) or None
  vals=(id,id.split('-')[0],tipo,cat,' '.join(title),org,place,postal,local,canton,mun,co,why,deadline,url,ctx.get('projectId'),ctx.get('lotId'),cpv,head+'\n'+body.strip())
  c.execute('INSERT OR IGNORE INTO publicaciones(id,proyecto,tipo,categoria,titulo,organismo,lugar_ejecucion,cp_postal,localidad_ejecucion,canton_ejecucion,municipio_organismo,canton_organismo,fundamento_origen,plazo,url,uuid_proyecto,uuid_lote,cpv,texto_original) VALUES('+','.join('?'*len(vals))+')',vals); c.execute('INSERT OR IGNORE INTO fuentes VALUES(?,?)',(id,mid))
 return len(blocks)

def main():
 a=argparse.ArgumentParser(description='Gestión local de publicaciones SIMAP'); a.add_argument('--version', action='version', version='SIMAP '+VERSION); a.add_argument('--db',default=str(Path(__file__).resolve().parent/'simap.sqlite')); sub=a.add_subparsers(dest='cmd',required=True)
 i=sub.add_parser('importar',help='Importar EML o ZIP'); i.add_argument('archivos',nargs='*'); i.add_argument('--carpeta',default=str(Path(__file__).resolve().parent)); i.add_argument('--recursivo',action='store_true'); i.add_argument('--listar',action='store_true')
 b=sub.add_parser('buscar'); b.add_argument('palabras',nargs='*'); b.add_argument('--canton'); b.add_argument('--municipio'); b.add_argument('--organismo'); b.add_argument('--tipo'); b.add_argument('--cpv'); b.add_argument('--csv'); b.add_argument('--origen',action='store_true'); b.add_argument('--detalle',action='store_true'); sub.add_parser('estadisticas'); e=sub.add_parser('enriquecer'); e.add_argument('--id',action='append'); e.add_argument('--limite',type=int,default=50); e.add_argument('--pausa',type=float,default=1); e.add_argument('--forzar',action='store_true'); e.add_argument('--reintentar',action='store_true'); i.add_argument('--enriquecer',action='store_true'); b.add_argument('--adjudicatario'); b.add_argument('--estado-api'); args=a.parse_args()
 if args.cmd=='importar':
  root=Path(args.carpeta).expanduser(); paths=set(); errors=0
  if not root.is_dir(): a.error('Carpeta inexistente: '+str(root))
  for pattern in args.archivos or ['*.eml']:
   p=Path(pattern).expanduser()
   if p.is_file(): matches=[p]
   else:
    pat=str(p if p.is_absolute() else root/p)
    if args.recursivo and not p.is_absolute() and p.parent==Path('.'): pat=str(root/'**'/p)
    matches=[Path(x) for x in glob.glob(pat,recursive=args.recursivo)]
   valid=[x.resolve() for x in matches if x.is_file() and x.suffix.lower() in ('.eml','.zip')]
   if not valid: print('Sin coincidencias: '+pattern,file=sys.stderr); errors+=1
   paths.update(valid)
  if args.listar:
   for p in sorted(paths): print(p)
   return int(bool(errors))
  c=db(args.db); before=c.execute('select count(*) from publicaciones').fetchone()[0]; ok=0
  def one(name,data):
   nonlocal ok,errors
   try:
    with c: count=ingest(c,name,data)
    print(f'{name}: {count} publicaciones procesadas'); ok+=1
   except Exception as e: print(f'ERROR {name}: {e}',file=sys.stderr); errors+=1
  for f in sorted(paths):
   try:
    if f.suffix.lower()=='.zip':
     with zipfile.ZipFile(f) as z:
      for n in z.namelist():
       if n.lower().endswith('.eml'): one(n,z.read(n))
    else: one(f.name,f.read_bytes())
   except Exception as e: print(f'ERROR {f}: {e}',file=sys.stderr); errors+=1
  after=c.execute('select count(*) from publicaciones').fetchone()[0]; print(f'Correos procesados: {ok}; errores: {errors}; nuevas publicaciones: {after-before}; total: {after}'); 
  if args.enriquecer and not errors: errors += enrich(c, None, 50, 1, False, False)
  c.close(); return int(bool(errors))
 c=db(args.db)
 if args.cmd=='enriquecer':
  try: return enrich(c,args.id,args.limite,args.pausa,args.forzar,args.reintentar)
  finally: c.close()
 if args.cmd=='estadisticas':
  for r in c.execute("select coalesce(estado_api,'pendiente'),count(*) from publicaciones group by estado_api"): print('API',*r)
  for t in ['correos','publicaciones','fuentes']: print(t,c.execute('select count(*) from '+t).fetchone()[0])
  for r in c.execute('select tipo,count(*) from publicaciones group by tipo'): print(*r)
 else:
  q='SELECT * FROM publicaciones WHERE 1=1'; params=[]
  for word in args.palabras:
   q+=" AND instr(norm(titulo||' '||coalesce(organismo,'')||' '||coalesce(texto_original,'')||' '||coalesce(descripcion,'')||' '||coalesce(adjudicatarios,'')),?)>0"; params.append(norm(word))
  for col,val in [('canton_organismo' if args.origen else 'canton_ejecucion',args.canton),('municipio_organismo' if args.origen else 'localidad_ejecucion',args.municipio),('organismo',args.organismo),('tipo',args.tipo),('cpv',args.cpv),('adjudicatarios',args.adjudicatario),('estado_api',args.estado_api)]:
   if val: q+=' AND instr(norm('+col+'),?)>0'; params.append(norm(val))
  cur=c.execute(q+' ORDER BY id DESC',params); rows=cur.fetchall()
  if args.csv:
   with open(args.csv,'w',encoding='utf-8-sig',newline='') as f:
    w=csv.writer(f); w.writerow([d[0] for d in cur.description]); w.writerows(rows)
  else:
   for r in rows: print(f"#{r['id']} | {r['tipo']} | {r['titulo']}\n  {r['organismo']} | {r['lugar_ejecucion'] or '—'} | plazo: {r['plazo'] or '—'}\n  {r['url']}\n")
  if args.detalle and not args.csv:
   for r in rows: print('\n'.join(f'{k}: {r[k] or "—"}' for k in r.keys()),'\n')
  print(f'{len(rows)} resultados',file=sys.stderr)

import time, urllib.request, urllib.error, uuid
API='https://www.simap.ch/api'
EXTRA=['descripcion','adjudicatarios','importes','duracion','json_api','fecha_api','estado_api','error_api','url_api','id_api']
def migrate(c):
 existing={r[1] for r in c.execute('pragma table_info(publicaciones)')}
 for key in EXTRA:
  if key not in existing: c.execute('alter table publicaciones add column '+key+' TEXT')
 c.commit()
def walk(x):
 if isinstance(x,dict):
  yield x
  for v in x.values(): yield from walk(v)
 elif isinstance(x,list):
  for v in x: yield from walk(v)
def dump(x): return json.dumps(x,ensure_ascii=False)
def extract(data):
 cpv=[]; desc=[]; duration=[]; winners=[]; prices=[]
 for obj in walk(data):
  for k,v in obj.items():
   if v is None: continue
   if k in ('cpvCode','cpvCodes','additionalCpvCodes','mainCpvCode'):
    cpv.extend(re.findall(r'(?<!\d)\d{8}(?:-\d)?(?!\d)',dump(v)))
   if k in ('description','procurementDescription','projectDescription','lotDescription','shortDescription','detailedDescription','descriptionOfProcurement') and v: desc.append(v if isinstance(v,str) else dump(v))
   if k in ('contractDeadlineType','contractPeriod','contractDays','contractDuration','contractStart','contractEnd','contractExtension','contractRenewal'): duration.append({k:v})
   if k in ('decision',) and isinstance(v,dict):
    for w in v.get('vendors') or []:
     if not isinstance(w,dict): continue
     if w.get('vendorName'): winners.append(w['vendorName'] if isinstance(w['vendorName'],str) else dump(w['vendorName']))
     if w.get('price') is not None: prices.append({'empresa':w.get('vendorName'),'precio':w['price'],'nota':w.get('note')})
    for field in ('totalPriceRange','totalPriceSelection','totalPrice'):
     if v.get(field) is not None: prices.append({field:v[field]})
 return ('; '.join(dict.fromkeys(cpv)) or None,'\n'.join(dict.fromkeys(desc)) or None,'; '.join(dict.fromkeys(winners)) or None,dump(prices) if prices else None,dump(duration) if duration else None)
def get_json(path,pause):
 time.sleep(max(1,pause)); req=urllib.request.Request(API+path,headers={'Accept':'application/json','User-Agent':'SIMAP-local-research/3.0'})
 with urllib.request.urlopen(req,timeout=30) as resp:
  if not resp.geturl().startswith(API+'/'): raise ValueError('Redireccion fuera de API')
  raw=resp.read(20_000_001)
  if len(raw)>20_000_000: raise ValueError('Respuesta demasiado grande')
 return json.loads(raw)
def candidates(data):
 for o in walk(data):
  for k in ('publicationId','currentPublicationId','latestPublicationId','newestPublicationId'):
   if isinstance(o.get(k),str):
    try: yield str(uuid.UUID(o[k]))
    except ValueError: pass
  if ('publicationNumber' in o or 'pubType' in o) and isinstance(o.get('id'),str):
   try: yield str(uuid.UUID(o['id']))
   except ValueError: pass
def matches(data,ref):
 for o in walk(data):
  for k in ('publicationNumber','pubNumber','publicationReference','referenceNumber','publicationIdNumber'):
   if str(o.get(k,'')).strip().lstrip('#')==ref: return True
 return False
def enrich(c,ids,limit,pause,force,retry):
 if limit<0 or pause<1: raise SystemExit('Limite >= 0; pausa >= 1 segundo')
 q='select * from publicaciones where 1=1'; args=[]
 if ids: q+=' and id in ('+','.join('?' for _ in ids)+')'; args+=ids
 if not force: q+=" and (estado_api is null"+(" or estado_api='error'" if retry else '')+")"
 q+=' order by id'; rows=c.execute(q,args).fetchall(); rows=rows[:limit] if limit else rows
 errors=0; headers={}; histories={}
 for r in rows:
  try:
   project=str(uuid.UUID(r['uuid_proyecto'])); lot=str(uuid.UUID(r['uuid_lote'])) if r['uuid_lote'] else None
   if project not in headers: headers[project]=get_json('/publications/v2/project/'+project+'/project-header',pause)
   candidates_all=list(dict.fromkeys(candidates(headers[project]))); found=None
   for pid in candidates_all:
    key=(pid,lot)
    if key not in histories:
     try: histories[key]=get_json('/publications/v1/publication/'+pid+'/past-publications'+('?lotId='+lot if lot else ''),pause)
     except urllib.error.HTTPError as exc:
      if exc.code not in (400,404): raise
      histories[key]=[]
    for cid in dict.fromkeys([pid]+list(candidates(histories[key]))):
     path='/publications/v1/project/'+project+'/publication-details/'+cid
     data=get_json(path,pause)
     if matches(data,r['id']): found=(cid,path,data); break
    if found: break
   if not found: raise ValueError('No se pudo verificar la referencia exacta; no se asignan datos de otra publicacion')
   cid,path,data=found; cpv,description,winners,prices,duration=extract(data)
   with c: c.execute("update publicaciones set cpv=coalesce(?,cpv),descripcion=?,adjudicatarios=?,importes=?,duracion=?,json_api=?,fecha_api=?,estado_api='ok',error_api=null,url_api=?,id_api=? where id=?",(cpv,description,winners,prices,duration,dump(data),datetime.now().astimezone().isoformat(),API+path,cid,r['id']))
   print(r['id'],'API ok',flush=True)
  except Exception as exc:
   errors+=1
   with c: c.execute("update publicaciones set estado_api='error',error_api=? where id=?",(str(exc),r['id']))
   print(r['id'],str(exc),file=sys.stderr,flush=True)
   if isinstance(exc,urllib.error.HTTPError) and exc.code in (401,403,429,503): break
 return int(bool(errors))

if __name__=='__main__': sys.exit(main())
