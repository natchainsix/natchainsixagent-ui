import asyncio, hashlib, hmac, json, os, re, sqlite3, time
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from agents import review
from judge import evaluate

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')
MODE=os.getenv('MODE','demo')
ORG=os.getenv('GITHUB_ORG','')
TOKEN=os.getenv('GITHUB_TOKEN','')
INTERVAL=max(5,int(os.getenv('POLL_SECONDS','30')))
DB=ROOT/'state.sqlite3'
WAKE=asyncio.Event()
FORCE=set()
META={}
JOBS={}
ACTIVE_JOBS={}

STATE={'running':False,'error':'','last_cycle':None,'phase':'starting','current_repo':None,'completed':0,'total':0,'agents':{},'events':[],'discovered':[]}

def activity(phase, message, **fields):
    STATE.update(phase=phase, **fields)
    STATE['events'] = ([{'time':time.time(),'phase':phase,'message':message}]+STATE['events'])[:40]

def agent_activity(name, status):
    STATE['agents'][name]=status
    activity('review', f'{name}: {status}')
EXT={'.py','.js','.ts','.java','.go','.cs','.sql','.yaml','.yml','.md','.toml'}

def connect():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    with connect() as c:
        c.executescript('CREATE TABLE IF NOT EXISTS scans(repo TEXT PRIMARY KEY, sha TEXT, result TEXT, updated REAL); CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY, repo TEXT, body TEXT, state TEXT, updated REAL);')

async def github(client,path,params=None):
    r=await client.get('https://api.github.com'+path,params=params,headers={'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json'} if TOKEN else {'Accept':'application/vnd.github+json'}); r.raise_for_status(); return r.json()

async def discover(client):
    if MODE=='demo':
        for name,provider in [('safelend','GitHub'),('quicklend','GitLab'),('lending-docs','Documentation')]:
            META[name]={'provider':provider,'owner':'NatChain Six (demo)','email':'owner@example.invalid'}
        return [(p.name,p) for p in sorted((ROOT/'demo').iterdir()) if p.is_dir()]
    result=[]
    if os.getenv('GITLAB_GROUP'):
        from urllib.parse import quote
        group=quote(os.environ['GITLAB_GROUP'],safe='')
        for page in range(1,101):
            r=await client.get(f'https://gitlab.com/api/v4/groups/{group}/projects',params={'include_subgroups':'true','per_page':100,'page':page},headers={'PRIVATE-TOKEN':os.getenv('GITLAB_TOKEN','')}); r.raise_for_status()
            batch=r.json()
            for item in batch:
                if item.get('archived') or not item.get('default_branch'): continue
                name='gitlab:'+item['path_with_namespace']; META[name]={'provider':'GitLab','owner':item['namespace']['full_path'],'email':None,'project_id':item['id']}; result.append((name,item['default_branch']))
            if len(batch)<100: break
    if os.getenv('DOCS_DIR'):
        root=Path(os.environ['DOCS_DIR']).resolve()
        if not root.is_dir(): raise ValueError('Invalid DOCS_DIR')
        META['documentation']={'provider':'Documentation','owner':os.getenv('DOCS_OWNER','Unassigned'),'email':None}; result.append(('documentation',root))
    if not ORG: return result
    if not re.fullmatch(r'[A-Za-z0-9-]+',ORG): raise ValueError('Configure GITHUB_ORG')
    for page in range(1,101):
        batch=await github(client,f'/orgs/{ORG}/repos',{'per_page':100,'page':page,'type':'all'})
        
        for r in batch:
            if not r['archived']:
                META[r['full_name']]={'provider':'GitHub','owner':r['owner']['login'],'email':None}; result.append((r['full_name'],r['default_branch']))
        if len(batch)<100: return result
    raise ValueError('Discovery page limit reached')

def eligible(path):
    p=Path(path); return p.suffix.lower() in EXT and not any(x.startswith('.') or x in {'node_modules','vendor','dist','build','secrets','credentials'} for x in p.parts)

async def snapshot(client,repo,ref,force=False):
    files=[]; skipped=[]; total=0
    if isinstance(ref,Path):
        entries=[(p.relative_to(ref).as_posix(),p.stat().st_size,p) for p in sorted(ref.rglob('*')) if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(ref.resolve()) and eligible(p.relative_to(ref).as_posix())]
        sha=hashlib.sha256(b''.join(str(p).encode()+p.read_bytes() for _,_,p in entries)).hexdigest()
    elif repo.startswith('gitlab:'):
        from urllib.parse import quote
        base=f"https://gitlab.com/api/v4/projects/{META[repo]['project_id']}"
        headers={'PRIVATE-TOKEN':os.getenv('GITLAB_TOKEN','')}
        r=await client.get(base+'/repository/branches/'+quote(ref,safe=''),headers=headers); r.raise_for_status(); sha=r.json()['commit']['id']
        entries=[]
        for page in range(1,101):
            r=await client.get(base+'/repository/tree',params={'ref':sha,'recursive':'true','per_page':100,'page':page},headers=headers); r.raise_for_status(); batch=r.json()
            entries.extend((x['path'],0,x['id']) for x in batch if x['type']=='blob' and x.get('mode')!='120000' and eligible(x['path']))
            if len(batch)<100: break
        else: skipped.append('GitLab tree page limit')
    else:
        from urllib.parse import quote
        commit=await github(client,f'/repos/{repo}/commits/{quote(ref,safe="")}'); sha=commit['sha']
        with connect() as c:
            old=c.execute('SELECT sha FROM scans WHERE repo=?',(repo,)).fetchone()
        if not force and old and old['sha']==sha: return sha,None,[]
        tree=await github(client,f'/repos/{repo}/git/trees/{commit["commit"]["tree"]["sha"]}',{'recursive':1})
        if tree.get('truncated'): skipped.append('GitHub tree truncated')
        entries=[(x['path'],x.get('size',0),x['sha']) for x in tree['tree'] if x['type']=='blob' and x.get('mode')!='120000' and eligible(x['path'])]
    with connect() as c:
        old=c.execute('SELECT sha FROM scans WHERE repo=?',(repo,)).fetchone()
    if not force and old and old['sha']==sha: return sha,None,[]
    for path,size,source in entries:
        if size>40000 or total+size>200000 or len(files)>=40: skipped.append(path); continue
        try:
            if isinstance(ref,Path): content=source.read_text(encoding='utf-8')
            elif repo.startswith('gitlab:'):
                async with client.stream('GET',base+'/repository/blobs/'+source+'/raw',headers=headers) as response:
                    response.raise_for_status(); raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>40000: break
                if len(raw)>40000: skipped.append(path); continue
                content=raw.decode('utf-8'); size=len(raw)
            else:
                import base64
                blob=await github(client,f'/repos/{repo}/git/blobs/{source}'); content=base64.b64decode(blob['content']).decode('utf-8')
        except UnicodeError: skipped.append(path); continue
        # Basic secret screening is not a substitute for a DLP gateway.
        if re.search(r'-----BEGIN .*PRIVATE KEY-----|(?:api[_-]?key|password|secret|token)\s*[:=]\s*[\"\'][^\"\']{8,}',content,re.I): skipped.append(path+' (possible secret)'); continue
        if total+size>200000: skipped.append(path); continue
        total+=size; files.append({'path':path,'content':content})
    return sha,files,skipped

async def notify(client):
    url=os.getenv('ALERT_WEBHOOK_URL','')
    if not url: return
    with connect() as c: rows=c.execute("SELECT * FROM alerts WHERE state='pending' LIMIT 50").fetchall()
    for row in rows:
        try:
            r=await client.post(url,json={'alert_id':row['id'],**json.loads(row['body'])},headers={'Authorization':'Bearer '+os.getenv('ALERT_TOKEN',''),'Idempotency-Key':row['id']}); r.raise_for_status()
            with connect() as c: c.execute("UPDATE alerts SET state='sent' WHERE id=?",(row['id'],))
        except httpx.HTTPError: continue

async def cycle(client):
    errors=[]
    activity("discovery", "Discovering authorised repositories", completed=0,total=0,current_repo=None,agents={})
    discovered=await discover(client)
    STATE.update(total=len(discovered),discovered=[r for r,_ in discovered])
    for repo,ref in discovered:
        job=None
        try:
            activity("snapshot", f"Checking {repo}",current_repo=repo,agents={})
            job=ACTIVE_JOBS.pop(repo,None)
            force=repo in FORCE; FORCE.discard(repo)
            if job: JOBS[job].update(status='running',message='Collecting source and reviewing current revision')
            sha,files,skipped=await snapshot(client,repo,ref,force)
            if files is None:
                activity("snapshot", f"{repo}: unchanged commit")
                continue
            activity("review", f"Reviewing {len(files)} files in {repo}")
            findings,source=await review(client,repo,sha,files,MODE,agent_activity)
            activity("persist", f"Recording {len(findings)} findings for {repo}")
            result={'findings':findings,'source':source,'files_reviewed':len(files),'skipped':skipped,'scope':'eligible text files on default branch; bounded review','evaluation':evaluate(repo,files,skipped,findings,source)}
            with connect() as c:
                c.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?,?)',(repo,sha,json.dumps(result),time.time()))
                for f in findings:
                    if f['status']!='NON_COMPLIANT': continue
                    key=hashlib.sha256(json.dumps([repo,sha,f['file'],f['line_start'],f['title']],ensure_ascii=False).encode()).hexdigest()
                    # External alerts deliberately omit raw code evidence.
                    body={'repository':repo,'commit':sha,'finding':{k:v for k,v in f.items() if k!='evidence'},'source':source,'owner':META.get(repo,{}).get('owner'),'recipient':META.get(repo,{}).get('email')}
                    c.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)',(key,repo,json.dumps(body),'pending',time.time()))
            if job: JOBS[job].update(status='completed',message=f'Rescan complete: {sum(f["status"]=="NON_COMPLIANT" for f in findings)} risk signals remain. Absence of a finding is not proof of a fix.',commit=sha)
        except Exception as exc:
            if job: JOBS[job].update(status='failed',message='Review failed: '+type(exc).__name__)
            errors.append(repo+': '+type(exc).__name__)
            activity('error',errors[-1])
        finally: STATE['completed']+=1
    activity('notify','Checking notification queue',current_repo=None)
    await notify(client); STATE['error']='; '.join(errors); STATE['last_cycle']=time.time()

async def worker():
    async with httpx.AsyncClient(timeout=httpx.Timeout(90,connect=10),follow_redirects=False) as client:
        while True:
            STATE['running']=True
            try: await cycle(client)
            except Exception as exc: STATE['error']=type(exc).__name__
            finally:
                STATE['running']=False
                activity('idle','Cycle finished with errors' if STATE['error'] else 'Monitoring for changes',current_repo=None)
            try: await asyncio.wait_for(WAKE.wait(),INTERVAL)
            except asyncio.TimeoutError: pass
            WAKE.clear()

@asynccontextmanager
async def lifespan(app):
    init(); task=asyncio.create_task(worker())
    yield
    task.cancel(); await asyncio.gather(task,return_exceptions=True)

app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
@app.middleware('http')
async def headers(request,call_next):
    response=await call_next(request)
    response.headers.update({'Content-Security-Policy':"default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cache-Control':'no-store'})
    return response
@app.get('/')
async def home(): return FileResponse(ROOT/'static/index.html')
@app.get('/api/status')
async def status():
    with connect() as c:
        scans=[{**dict(r),'result':json.loads(r['result'])} for r in c.execute('SELECT * FROM scans ORDER BY repo')]
        pending=c.execute("SELECT count(*) FROM alerts WHERE state='pending'").fetchone()[0]
        sent=c.execute("SELECT count(*) FROM alerts WHERE state='sent'").fetchone()[0]
        alerts=[{**dict(r),'body':json.loads(r['body'])} for r in c.execute('SELECT * FROM alerts ORDER BY updated DESC LIMIT 500')]
    return {'metadata':META,'alerts':alerts,'relay_configured':bool(os.getenv('ALERT_WEBHOOK_URL')),'mode':MODE,**STATE,'repositories':scans,'pending_alerts':pending,'sent_alerts':sent,'poll_seconds':INTERVAL}
@app.post('/webhooks/github')
async def webhook(request:Request):
    secret=os.getenv('GITHUB_WEBHOOK_SECRET','')
    if not secret: raise HTTPException(503,'Webhook not configured')
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>1000000: raise HTTPException(413)
    expected='sha256='+hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,request.headers.get('X-Hub-Signature-256','')): raise HTTPException(401)
    if request.headers.get('X-GitHub-Event') in {'push','repository','installation_repositories','pull_request'}: WAKE.set()
    return {'accepted':True}
@app.post('/api/rescan')
async def rescan(request:Request):
    if request.headers.get('origin') != str(request.base_url).rstrip('/'): raise HTTPException(403,'Same-origin request required')
    body=await request.json(); repo=body.get('repository')
    if not isinstance(repo,str) or repo not in STATE['discovered']: raise HTTPException(404,'Unknown source')
    if repo in ACTIVE_JOBS: return {'job':ACTIVE_JOBS[repo]}
    if len(JOBS)>=200:
        for key in list(JOBS):
            if JOBS[key]['status'] in {'completed','failed'}: del JOBS[key]; break
        else: raise HTTPException(429,'Queue full')
    import uuid
    job=uuid.uuid4().hex; JOBS[job]={'status':'queued','message':'Rescan queued','repository':repo}; ACTIVE_JOBS[repo]=job; FORCE.add(repo); WAKE.set(); return {'job':job}
@app.get('/api/rescan/{job}')
async def job_status(job:str):
    if job not in JOBS: raise HTTPException(404)
    return JOBS[job]
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
