'use strict';
const $=id=>document.getElementById(id), n=(tag,text='',cls='')=>{const e=document.createElement(tag);e.textContent=text;e.className=cls;return e;};
let data,selected,signature='',busy=false;
const risk=f=>f.status==='NON_COMPLIANT';
function render(){const repos=data.repositories, names=data.discovered, meta=data.metadata; if(!names.includes(selected))selected=names[0];$('status').textContent=data.error?'Worker warning: '+data.error:data.running?`${data.phase} · ${data.current_repo||'Background worker'}`:'Monitoring for changes';$('mode').textContent=(data.agents_configured||data.compliance_api_configured)?'LIVE · Real FCA/CCA Compliance Agent connected':data.mode==='demo'?'DEMO · synthetic checks and simulated providers':'LIVE · configured agent endpoints';$('radar').classList.toggle('active',data.running);$('progress').max=Math.max(1,data.total);$('progress').value=data.completed;$('agents').textContent=Object.entries(data.agents).map(([k,v])=>`${k}: ${v}`).join(' · ');$('pipeline').replaceChildren(...['discovery','snapshot','review','persist','notify'].map(k=>n('div',k,'stage'+(data.running&&data.phase===k?' active':''))));$('metrics').replaceChildren(...[['Sources discovered',names.length],['Sources reviewed',repos.filter(r=>names.includes(r.repo)).length],['Open risk signals',repos.filter(r=>names.includes(r.repo)).flatMap(r=>r.result.findings).filter(risk).length],['Relay accepted / pending',`${data.sent_alerts} / ${data.pending_alerts}`]].map(([k,v])=>{const t=n('div','','tile');t.append(n('small',k),n('strong',String(v)));return t;}));$('events').replaceChildren(...data.events.map(e=>n('div',new Date(e.time*1000).toLocaleTimeString()+' · '+e.message,'event')));const sig=JSON.stringify([names,meta,repos,data.alerts,selected,$('filter').value]);if(sig===signature)return;signature=sig;$('sources').replaceChildren();for(const provider of ['GitHub','Pull Requests','GitLab','Documentation']){$('sources').append(n('div',provider,'group'));const group=names.filter(x=>meta[x]?.provider===provider);if(!group.length)$('sources').append(n('small','No sources discovered'));for(const name of group){const r=repos.find(x=>x.repo===name),b=n('button',name,'source');b.setAttribute('aria-pressed',String(name===selected));b.append(n('small',r?`${r.result.findings.filter(risk).length} risk signals`:'Awaiting review'));b.onclick=()=>{selected=name;render();};$('sources').append(b);}}const r=repos.find(x=>x.repo===selected),m=meta[selected]||{};renderJudge(r);$('heading').textContent=selected||'Awaiting discovery';$('metadata').textContent=`Owner: ${m.owner||'Unknown'} · Email: ${m.email||'Not configured; relay routing required'}`;$('findings').replaceChildren();if(!r){$('findings').append(n('p','Awaiting first review'));return;}$('findings').append(n('p',`${r.result.source} · revision ${r.sha.slice(0,12)} · ${r.result.files_reviewed} files · ${r.result.skipped.length} skipped · ${new Date(r.updated*1000).toLocaleString()}`,'muted'));if(r.result.skipped.length){const d=n('details');d.append(n('summary','Coverage limitations'),n('pre',r.result.skipped.join('\n')));$('findings').append(d);}const fs=r.result.findings.filter(f=>$('filter').value==='ALL'||f.severity===$('filter').value);if(!fs.length)$('findings').append(n('p','No matching findings. Not a compliance certification.'));for(const f of fs){const a=n('article','',risk(f)?'risk':'');const alert=data.alerts.find(x=>x.repo===selected&&x.body.commit===r.sha&&x.body.finding.title===f.title&&x.body.finding.file===f.file&&x.body.finding.line_start===f.line_start);a.append(n('small',`${f.severity} · ${f.status} · ${f.agent}`),n('h3',f.title),n('p',`${f.file}:${f.line_start} · ${f.regulation}`),n('p','Email alert: '+(!risk(f)?'Not applicable':alert?.state==='sent'?'Relay accepted; email delivery unverified':alert?'Queued'+(data.relay_configured?'':' — relay not configured'):'Not recorded')));const d=n('details');d.append(n('summary','Issue evidence and recommended fix'),n('pre',f.evidence),n('p',f.remediation),n('p','Owner: '+(m.owner||'Unknown')));const b=n('button','Copy recommendation');b.onclick=async()=>{try{await navigator.clipboard.writeText(f.remediation);b.textContent='Copied';}catch{b.textContent='Copy unavailable';}};d.append(b);a.append(d);$('findings').append(a);}}
async function api(url,opts={}){const r=await fetch(url,{...opts,signal:AbortSignal.timeout(10000),cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);return r.json();}async function refresh(){try{data=await api('/api/status');render();}catch{$('status').textContent='Disconnected — results may be stale';$('radar').classList.remove('active');}finally{setTimeout(refresh,1500);}}function say(text,user=false){$('messages').append(n('div',text,'message'+(user?' user':'')));$('messages').scrollTop=$('messages').scrollHeight;}function toggle(open){$('chat').hidden=!open;$('chat-toggle').setAttribute('aria-expanded',String(open));if(open)$('prompt').focus();}$('chat-toggle').onclick=()=>toggle($('chat').hidden);$('close').onclick=()=>toggle(false);$('filter').onchange=()=>{if(data)render();};async function rescan(){if(busy||!selected)return;busy=true;$('rescan').disabled=true;const repo=selected;const before=data.repositories.find(r=>r.repo===repo)?.result.findings.filter(risk)||[];say('Requesting fresh agent review of '+repo);try{const {job}=await api('/api/rescan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({repository:repo})});let last='';for(let i=0;i<600;i++){const j=await api('/api/rescan/'+job);if(j.message!==last){say(j.message);last=j.message;}if(j.status==='failed')break;if(j.status==='completed'){const latest=await api('/api/status');const r=latest.repositories.find(x=>x.repo===repo);if(r&&r.sha===j.commit){const after=r.result.findings.filter(risk);const missing=before.filter(f=>!after.some(a=>a.title===f.title&&a.file===f.file));say(`${after.length} current risk signals. ${missing.length} previous signals not redetected. ${r.result.skipped.length} skipped files. Human verification required before marking fixed.`);data=latest;render();}break;}await new Promise(resolve=>setTimeout(resolve,1500));}}catch(e){say('Rescan status unavailable: '+e.message);}finally{busy=false;$('rescan').disabled=false;}}$('rescan').onclick=rescan;$('chat-form').onsubmit=e=>{e.preventDefault();const q=$('prompt').value.trim();$('prompt').value='';if(!q)return;say(q,true);if(/github\.com\//i.test(q)&&data?.compliance_api_configured){runBackendTarget({mode:'chat',message:q},'Chat request: '+q);say('Sent to live FCA/CCA compliance agent — see Run Output panel.');}else if(/rescan|verify|scan|fix/i.test(q))rescan();else if(/status/i.test(q))say($('status').textContent);else say('Supported commands: rescan, verify fix, status, or paste a GitHub repo/PR URL.');};say('Select a source and ask me to rescan or verify a fix.');refresh();

let backendLoaded=false,backendRunBusy=false;
function badge(text,cls=''){const s=n('span',text,'badge'+(cls?' '+cls:''));return s;}

function renderBackendReport(report,deliverable){
  const box=$('backend-report-view');
  box.replaceChildren();
  if(!report){return;}
  const head=n('div','','target-card');
  const verdictCls=report.overall_verdict==='compliant'?'ok':'warn';
  head.append(badge(report.overall_verdict||'unknown',verdictCls),badge(report.report_id||''),n('strong',report.title||report.target_id||'Compliance Report'));
  head.append(n('p',report.executive_summary||''));
  head.append(n('small',`Target: ${report.target_id} · Units: ${report.units_triaged_in}/${report.units_total} analysed (${report.units_skipped} skipped) · Findings: ${(report.findings||[]).length}`));
  if(deliverable){
    head.append(n('p',`Deliverable (${deliverable.status}): ${deliverable.summary}`));
    if((deliverable.artifacts||[]).length){
      const ul=n('div');
      for(const a of deliverable.artifacts){
        if(/^https?:\/\//i.test(a)){
          const link=n('a',a);link.href=a;link.target='_blank';link.rel='noopener noreferrer';
          const row=n('div');row.append(link);ul.append(row);
        }else{
          ul.append(n('div',a,'muted'));
        }
      }
      head.append(ul);
    }
  }
  box.append(head);
  for(const f of (report.findings||[])){
    const isRisk=f.status==='non_compliant'||f.status==='potential_breach';
    const art=n('article','',isRisk?'risk':'');
    const loc=f.location||{};
    const where=loc.file_path?`${loc.file_path}${loc.start_line?':'+loc.start_line:''}`:(loc.document_name||loc.reference||'');
    const fcaRefs=(f.fca_citations||[]).map(c=>c.reference).filter(Boolean);
    const ccaRefs=(f.cca_citations||[]).map(c=>c.reference).filter(Boolean);
    art.append(n('small',`${String(f.severity||'').toUpperCase()} · ${f.status} · ${where}`),n('h3',f.title||'Finding'),n('p',f.description||''));
    if(fcaRefs.length)art.append(n('p','FCA Handbook: '+fcaRefs.join(', ')));
    if(ccaRefs.length)art.append(n('p','Consumer Credit Act: '+ccaRefs.join(', ')));
    if(f.fca_vs_cca_comparison)art.append(n('p','FCA vs CCA: '+f.fca_vs_cca_comparison,'muted'));
    const det=n('details');
    det.append(n('summary','Evidence & remediation'));
    if(f.evidence_excerpt)det.append(n('pre',f.evidence_excerpt));
    if(f.remediation_guidance)det.append(n('p',f.remediation_guidance));
    if(f.suggested_change)det.append(n('pre',f.suggested_change));
    art.append(det);
    box.append(art);
  }
}

async function loadBackendReport(reportId){
  try{
    $('backend-run-status').textContent='Loading report '+reportId+'…';
    const res=await api('/api/backend/reports/'+encodeURIComponent(reportId));
    $('backend-run-status').textContent='Viewing persisted report '+reportId;
    renderBackendReport(res.report,null);
  }catch(e){
    $('backend-run-status').textContent='Could not load report: '+e.message;
  }
}

async function loadBackendFindings(){
  const box=$('backend-reports');
  try{
    const res=await api('/api/backend/findings?limit=15');
    box.replaceChildren();
    if(!(res.reports||[]).length){
      box.append(n('p','No persisted reports yet. Run a scan above.','muted'));
      return;
    }
    for(const rep of res.reports){
      const b=n('button','','report-btn');
      b.type='button';
      b.append(badge(rep.overall_verdict,rep.overall_verdict==='compliant'?'ok':'warn'),document.createTextNode(`${rep.report_id} · ${rep.target_id} (${rep.findings_count} findings)`));
      b.onclick=()=>loadBackendReport(rep.report_id);
      box.append(b);
    }
  }catch(e){
    box.replaceChildren(n('p','Unable to load backend reports: '+e.message,'muted'));
  }
}

async function runBackendTarget(payload,label){
  if(backendRunBusy)return;
  backendRunBusy=true;
  $('backend-run-status').textContent='Starting '+label+'…';
  try{
    const run=await api('/api/backend/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    $('backend-run-status').textContent=`Run ${run.run_id} (${run.status}) · ${label}`;
    for(let i=0;i<600;i++){
      await new Promise(resolve=>setTimeout(resolve,1500));
      const cur=await api('/api/backend/runs/'+encodeURIComponent(run.run_id));
      $('backend-run-status').textContent=`Run ${cur.run_id} (${cur.status}) · ${label}`;
      if(cur.status==='failed'){
        $('backend-run-status').textContent=`Run ${cur.run_id} failed: ${cur.error||'Unknown error'}`;
        break;
      }
      if(cur.status==='completed'){
        $('backend-run-status').textContent=`Completed ${cur.run_id} · ${label}`;
        if(cur.findings_report)renderBackendReport(cur.findings_report,cur.deliverable_result);
        else if(cur.reply){$('backend-report-view').replaceChildren(n('pre',cur.reply));}
        await loadBackendFindings();
        break;
      }
    }
  }catch(e){
    $('backend-run-status').textContent='Run request failed: '+e.message;
  }finally{
    backendRunBusy=false;
  }
}

async function loadBackendTargets(){
  const list=$('backend-targets');
  list.replaceChildren(n('p','Discovering repositories and pull requests…','muted'));
  try{
    const [health,targets]=await Promise.all([api('/api/backend/health'),api('/api/backend/targets?include_prs=true&pr_state=open')]);
    const hBox=$('backend-health');
    hBox.replaceChildren(
      badge('FCA MCP: '+(health.fca_mcp_reachable?'UP':'DOWN'),health.fca_mcp_reachable?'ok':'warn'),
      badge('CCA MCP: '+(health.cca_mcp_reachable?'UP':'DOWN'),health.cca_mcp_reachable?'ok':'warn'),
      n('small',`Org: ${targets.org||health.default_org} · Model: ${health.model_name||''}`)
    );
    list.replaceChildren();
    for(const w of (targets.warnings||[])){
      list.append(n('small',w,'muted'));
    }
    if(!(targets.repositories||[]).length){
      list.append(n('p','No repositories discovered. Paste any GitHub repo or PR URL above.','muted'));
    }
    for(const repo of (targets.repositories||[])){
      const card=n('div','','target-card');
      const head=n('div','','target-card-head');
      const titleWrap=n('div');
      titleWrap.append(n('strong',repo.full_name),n('small',' ('+repo.default_branch+')'));
      const scanBtn=n('button','Scan Repo');
      scanBtn.type='button';
      scanBtn.onclick=()=>runBackendTarget({mode:'codebase_review',repo_url:repo.url,branch:repo.default_branch||'main',create_pr:$('backend-write-github').checked},repo.full_name);
      head.append(titleWrap,scanBtn);
      card.append(head);
      if(repo.description)card.append(n('small',repo.description));
      if((repo.pull_requests||[]).length){
        for(const pr of repo.pull_requests){
          const row=n('div','','pr-row');
          row.append(n('span',`PR #${pr.number}: ${pr.title}`));
          const prBtn=n('button','Review PR');
          prBtn.type='button';
          prBtn.onclick=()=>runBackendTarget({mode:'pr_review',pr_url:pr.url,post_to_github:$('backend-write-github').checked},`${repo.full_name}#${pr.number}`);
          row.append(prBtn);
          card.append(row);
        }
      }else{
        card.append(n('div','No open PRs','muted'));
      }
      list.append(card);
    }
  }catch(e){
    $('backend-health').textContent='Compliance backend offline: '+e.message;
    list.replaceChildren(n('p','Could not reach compliance backend targets endpoint.','muted'));
  }
}

$('refresh-targets').onclick=()=>{loadBackendTargets();loadBackendFindings();};
$('custom-target-form').onsubmit=e=>{
  e.preventDefault();
  const url=$('custom-target-url').value.trim();
  if(!url)return;
  const writeGh=$('backend-write-github').checked;
  if(/\/pull\/\d+/i.test(url)){
    runBackendTarget({mode:'pr_review',pr_url:url,post_to_github:writeGh},url);
  }else{
    runBackendTarget({mode:'codebase_review',repo_url:url,create_pr:writeGh},url);
  }
};

const _origRender=render;
render=function(){
  _origRender();
  if(data?.compliance_api_configured){
    $('backend-panel').hidden=false;
    if(!backendLoaded){backendLoaded=true;loadBackendTargets();loadBackendFindings();}
  }
};

function renderJudge(r){const panel=$('judge-results');panel.replaceChildren();const e=r?.result.evaluation;if(!e){panel.append(n('p','No evaluation yet. Rescan this source.'));return;}panel.append(n('p',r.repo+' · '+r.sha.slice(0,12)+' · '+e.reason));const grid=n('div','','metrics');for(const [label,key] of [['Benchmark accuracy','accuracy'],['Precision','precision'],['Recall','recall'],['F1','f1'],['Literal evidence match','evidence_match']]){const t=n('div','','tile');t.append(n('small',label),n('strong',e[key]==null?'N/A':e[key]+'%'));grid.append(t);}panel.append(grid);if(e.tp!=null)panel.append(n('p',`True positives ${e.tp} · False positives ${e.fp} · False negatives ${e.fn} · True negatives ${e.tn}`));panel.append(n('small','File-level synthetic benchmark; evidence match is a literal line check, not citation or fix correctness.'));}
$('judge-button').onclick=()=>{const p=$('judge-results');p.hidden=!p.hidden;$('judge-button').textContent=p.hidden?'Show evaluation for selected source':'Hide evaluation';if(data)renderJudge(data.repositories.find(r=>r.repo===selected));};
