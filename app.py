import asyncio, hashlib, hmac, json, os, re, sqlite3, time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
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
AGENT_TIMEOUT=max(30,int(os.getenv('AGENT_TIMEOUT_SECONDS','300')))
COMPLIANCE_API_URL=os.getenv('COMPLIANCE_API_URL','').rstrip('/')
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
    if MODE=='backend':
        url=_validated_backend_url('/api/targets')
        token=os.getenv('AGENT_TOKEN','')
        headers={'Authorization':'Bearer '+token} if token else {}
        r=await client.get(url,params={'include_prs':'true','pr_state':'open'},headers=headers); r.raise_for_status()
        payload=r.json()
        result=[]
        for repo in payload.get('repositories') or []:
            slug=repo.get('full_name') or ''
            if not slug: continue
            owner=slug.split('/')[0] if '/' in slug else (payload.get('org') or 'GitHub')
            META[slug]={'provider':'GitHub','owner':owner,'email':None,'url':repo.get('url') or f'https://github.com/{slug}','branch':repo.get('default_branch') or 'main','kind':'repo'}
            result.append((slug,META[slug]))
            for pr in repo.get('pull_requests') or []:
                pr_num=pr.get('number')
                if pr_num is None: continue
                key=f'{slug}#{pr_num}'
                META[key]={'provider':'Pull Requests','owner':pr.get('author') or owner,'email':None,'url':pr.get('url') or f'https://github.com/{slug}/pull/{pr_num}','repo':slug,'pr_number':pr_num,'kind':'pr','title':pr.get('title') or f'PR #{pr_num}'}
                result.append((key,META[key]))
        return result
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

def _backend_report_to_scan(report:dict)->tuple[str,dict]:
    status_map={'non_compliant':'NON_COMPLIANT','potential_breach':'NON_COMPLIANT','needs_review':'NEEDS_CONTEXT','observation':'COMPLIANT_WITHIN_SCOPE'}
    sev_map={'critical':'CRITICAL','high':'HIGH','medium':'MEDIUM','low':'LOW','info':'LOW'}
    findings=[]
    for f in report.get('findings') or []:
        loc=f.get('location') or {}
        refs=[c.get('reference') for c in (*(f.get('fca_citations') or []), *(f.get('cca_citations') or [])) if isinstance(c,dict) and c.get('reference')]
        findings.append({
            'status':status_map.get(f.get('status'),'NON_COMPLIANT'),
            'severity':sev_map.get(f.get('severity'),'MEDIUM'),
            'title':f.get('title') or 'Compliance finding',
            'file':loc.get('file_path') or loc.get('document_name') or loc.get('reference') or 'unknown',
            'line_start':int(loc.get('start_line') or 1),
            'regulation':', '.join(dict.fromkeys(refs)) or 'FCA Handbook / Consumer Credit Act 1974',
            'evidence':(f.get('evidence_excerpt') or f.get('description') or '').strip()[:4000],
            'remediation':(f.get('remediation_guidance') or f.get('description') or '').strip()[:4000],
            'agent':'fca_compliance',
        })
    sha=str(report.get('report_id') or 'latest')
    result={'findings':findings,'source':'LIVE FCA/CCA Compliance Agent','files_reviewed':int(report.get('units_triaged_in') or report.get('units_total') or 0),'skipped':[],'scope':'Backend FCA Handbook & CCA 1974 compliance pipeline','evaluation':None}
    return sha,result

def _match_backend_target(target_id:str,repo_key:str,meta:dict)->bool:
    tid=(target_id or '').rstrip('/').lower()
    url=(meta.get('url') or '').rstrip('/').lower()
    return tid==repo_key.lower() or (bool(url) and tid==url)

def _store_backend_scan(repo:str,sha:str,result:dict,meta:dict):
    now=time.time()
    with connect() as c:
        c.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?,?)',(repo,sha,json.dumps(result),now))
        for f in result.get('findings') or []:
            if f.get('status')!='NON_COMPLIANT': continue
            key=hashlib.sha256(json.dumps([repo,sha,f['file'],f['line_start'],f['title']],ensure_ascii=False).encode()).hexdigest()
            body={'repository':repo,'commit':sha,'finding':{k:v for k,v in f.items() if k!='evidence'},'source':result.get('source'),'owner':meta.get('owner'),'recipient':meta.get('email')}
            c.execute('INSERT OR IGNORE INTO alerts VALUES(?,?,?,?,?)',(key,repo,json.dumps(body),'pending',now))

async def _cycle_backend(client,discovered):
    errors=[]
    token=os.getenv('AGENT_TOKEN','')
    headers={'Authorization':'Bearer '+token} if token else {}
    reports_cache=[]
    try:
        r=await client.get(_validated_backend_url('/api/findings'),params={'limit':100},headers=headers); r.raise_for_status()
        f_data=r.json()
        reports_cache=f_data.get('reports') or []
    except Exception as exc:
        errors.append('findings sync: '+type(exc).__name__)
    for repo,meta in discovered:
        job=None
        try:
            job=ACTIVE_JOBS.pop(repo,None)
            force=repo in FORCE; FORCE.discard(repo)
            if force:
                activity('review',f'Triggering live backend review for {repo}',current_repo=repo,agents={'fca_compliance':'running'})
                if job: JOBS[job].update(status='running',message=f'Running live FCA/CCA review of {repo} on backend')
                run_payload={'mode':'pr_review','pr_url':meta['url'],'post_to_github':False} if meta.get('kind')=='pr' else {'mode':'codebase_review','repo_url':meta['url'],'branch':meta.get('branch','main'),'create_pr':False}
                start=await client.post(_validated_backend_url('/api/runs'),json=run_payload,headers=headers); start.raise_for_status()
                run_id=start.json()['run_id']
                run_res={}
                seen_seq=0
                stage_to_phase={'inbound':'snapshot','triage':'review','analyze':'review','synthesize':'review','review_gate':'review','outbound':'persist','completed':'persist','failed':'error'}
                for _ in range(max(120,int(AGENT_TIMEOUT/1.0))):
                    await asyncio.sleep(1.0)
                    poll=await client.get(_validated_backend_url(f'/api/runs/{run_id}'),headers=headers); poll.raise_for_status()
                    run_res=poll.json()
                    for evt in run_res.get('events') or []:
                        seq=int(evt.get('seq') or 0)
                        if seq<=seen_seq: continue
                        seen_seq=seq
                        phase=stage_to_phase.get(evt.get('stage'),'review')
                        msg=str(evt.get('message') or '')
                        if msg:
                            activity(phase,f'{repo}: {msg}',current_repo=repo)
                            if job: JOBS[job].update(status='running',message=msg,events=(run_res.get('events') or [])[-30:])
                    if run_res.get('status') in {'completed','failed'}: break
                if run_res.get('status')!='completed' or not run_res.get('findings_report'):
                    raise RuntimeError(run_res.get('error') or 'Backend review did not complete')
                sha,result=_backend_report_to_scan(run_res['findings_report'])
                STATE['agents']['fca_compliance']='completed'
                activity('persist',f'Recorded {len(result["findings"])} findings for {repo}')
                _store_backend_scan(repo,sha,result,meta)
                if job: JOBS[job].update(status='completed',message=f'Rescan complete: {sum(f["status"]=="NON_COMPLIANT" for f in result["findings"])} risk signals remain.',commit=sha)
            else:
                match=next((rep for rep in reports_cache if _match_backend_target(rep.get('target_id',''),repo,meta)),None)
                if match:
                    rep_id=match['report_id']
                    with connect() as c:
                        old=c.execute('SELECT sha FROM scans WHERE repo=?',(repo,)).fetchone()
                    if not old or old['sha']!=rep_id:
                        full=await client.get(_validated_backend_url(f'/api/reports/{rep_id}'),headers=headers); full.raise_for_status()
                        sha,result=_backend_report_to_scan(full.json()['report'])
                        _store_backend_scan(repo,sha,result,meta)
                        activity('persist',f'Synced backend report {rep_id} for {repo}')
        except Exception as exc:
            STATE['agents']['fca_compliance']='failed'
            if job: JOBS[job].update(status='failed',message='Review failed: '+str(exc)[:180])
            errors.append(repo+': '+type(exc).__name__)
            activity('error',errors[-1])
        finally:
            STATE['completed']+=1
    return errors

async def cycle(client):
    errors=[]
    activity("discovery", "Discovering authorised repositories", completed=0,total=0,current_repo=None,agents={})
    discovered=await discover(client)
    STATE.update(total=len(discovered),discovered=[r for r,_ in discovered])
    if MODE=='backend':
        errors=await _cycle_backend(client,discovered)
        activity('notify','Checking notification queue',current_repo=None)
        await notify(client); STATE['error']='; '.join(errors); STATE['last_cycle']=time.time()
        return
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
    async with httpx.AsyncClient(timeout=httpx.Timeout(AGENT_TIMEOUT,connect=10),follow_redirects=False) as client:
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
    try:
        agents_configured=bool(json.loads(os.getenv('AGENTS_JSON','{}')))
    except json.JSONDecodeError:
        agents_configured=False
    backend_url=os.getenv('COMPLIANCE_API_URL',COMPLIANCE_API_URL).rstrip('/')
    return {'metadata':META,'alerts':alerts,'relay_configured':bool(os.getenv('ALERT_WEBHOOK_URL')),'mode':MODE,'agents_configured':agents_configured,'compliance_api_configured':bool(backend_url),**STATE,'repositories':scans,'pending_alerts':pending,'sent_alerts':sent,'poll_seconds':INTERVAL}

def _validated_backend_url(path:str)->str:
    base=os.getenv('COMPLIANCE_API_URL',COMPLIANCE_API_URL).rstrip('/')
    if not base: raise HTTPException(503,'COMPLIANCE_API_URL is not configured')
    parsed=urlsplit(base)
    if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in {'localhost','127.0.0.1','::1'}):
        raise HTTPException(400,'COMPLIANCE_API_URL requires HTTPS or loopback')
    return base+path

async def _proxy_backend(method:str,path:str,*,params:dict|None=None,json_body:dict|None=None):
    url=_validated_backend_url(path)
    token=os.getenv('AGENT_TOKEN','')
    headers={'Authorization':'Bearer '+token} if token else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(60,connect=10),follow_redirects=False) as client:
            r=await client.request(method,url,params=params,json=json_body,headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502,f'Compliance backend unreachable: {type(exc).__name__}') from exc
    if r.status_code>=400:
        detail=r.text[:400] if r.text else f'Backend returned HTTP {r.status_code}'
        raise HTTPException(r.status_code,detail)
    return r.json()

@app.get('/api/backend/health')
async def backend_health():
    return await _proxy_backend('GET','/health')

@app.get('/api/backend/targets')
async def backend_targets(org:str|None=None,include_prs:bool=True,pr_state:str='open'):
    params={'include_prs':str(include_prs).lower(),'pr_state':pr_state}
    if org: params['org']=org
    return await _proxy_backend('GET','/api/targets',params=params)

@app.get('/api/backend/findings')
async def backend_findings(target_id:str|None=None,source_type:str|None=None,severity:str|None=None,limit:int=50):
    params={'limit':limit}
    if target_id: params['target_id']=target_id
    if source_type: params['source_type']=source_type
    if severity: params['severity']=severity
    return await _proxy_backend('GET','/api/findings',params=params)

@app.get('/api/backend/reports/{report_id}')
async def backend_report(report_id:str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',report_id): raise HTTPException(400,'Invalid report ID')
    return await _proxy_backend('GET',f'/api/reports/{report_id}')

@app.post('/api/backend/runs')
async def backend_create_run(request:Request):
    if request.headers.get('origin') != str(request.base_url).rstrip('/'): raise HTTPException(403,'Same-origin request required')
    body=await request.json()
    return await _proxy_backend('POST','/api/runs',json_body=body)

@app.get('/api/backend/runs')
async def backend_list_runs(limit:int=30):
    return await _proxy_backend('GET','/api/runs',params={'limit':limit})

@app.get('/api/backend/runs/{run_id}')
async def backend_get_run(run_id:str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id): raise HTTPException(400,'Invalid run ID')
    return await _proxy_backend('GET',f'/api/runs/{run_id}')

@app.get('/api/backend/runs/{run_id}/events')
async def backend_stream_run_events(run_id:str):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id): raise HTTPException(400,'Invalid run ID')
    url=_validated_backend_url(f'/api/runs/{run_id}/events')
    token=os.getenv('AGENT_TOKEN','')
    headers={'Authorization':'Bearer '+token} if token else {}
    async def relay():
        async with httpx.AsyncClient(timeout=httpx.Timeout(AGENT_TIMEOUT+30,connect=10),follow_redirects=False) as client:
            async with client.stream('GET',url,headers=headers) as r:
                r.raise_for_status()
                async for chunk in r.aiter_bytes():
                    yield chunk
    return StreamingResponse(relay(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
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
