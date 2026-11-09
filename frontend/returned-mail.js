/* Account-folder imports and evidence review; server owns identity and scope. */
(function(root){
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let cfg,seq=0,owner=null,docs=[],next=null,reasons={},dirty=new Set(),saving=false;
  const el=id=>document.getElementById(id);
  function accountFromPath(path){
    const parts=String(path).replaceAll('\\','/').split('/');
    if(parts.length<2||/^[A-Za-z]:/.test(path)||parts.some(p=>!p||p==='.'||p==='..'))throw Error('Select files inside account-number folders.');
    const account=parts.at(-2).trim().toUpperCase();
    if(!/^[A-Z]?\d{1,20}$/.test(account))throw Error('Each file must be directly inside its account-number folder.');
    return account;
  }
  function canEdit(doc){const item=cfg.selected();return !!(item&&doc.intelligence_item_id===item.id&&item.work_claim?.mine&&!['RESOLVED','DISMISSED'].includes(item.status));}
  function access(){
    for(const doc of docs){const fieldset=document.getElementById('return-fields-'+doc.id);if(fieldset)fieldset.disabled=saving||!canEdit(doc);const hint=document.querySelector(`[data-return-access="${doc.id}"]`);if(hint)hint.textContent=canEdit(doc)?'':'Start review on this scan’s returned-mail queue item to edit it. Completed scans retain their history.';}
  }
  function addressText(a){return a?[a.line1,a.line2,a.unit,[a.city,a.state,a.zip].filter(Boolean).join(' ')].filter(Boolean).join('\n'):'';}
  function researchHtml(research){
    if(!research)return '<p class="subtle">Automatic research has not been queued for this historical scan.</p>';
    const result=research.result||{}, labels={QUEUED:'Research queued',RUNNING:'Research in progress',COMPLETE:'Research checked',PARTIAL:'Some research needs attention',FAILED:'Research failed',SKIPPED:'Verify account before research'};
    const crs=result.current_crs;
    const web=result.web||{};
    const sources=(web.sources||[]).filter(s=>{try{const u=new URL(s.url);return ['http:','https:'].includes(u.protocol)&&!u.username&&!u.password;}catch{return false;}});
    const summary=String(web.summary||'').replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,(match,label,url)=>{const i=sources.findIndex(s=>s.url===url);return i>=0?'[Source '+(i+1)+']':'[Unverified reference]';}).replace(/cite[^]*/g,'[See sources below]');
    return `<section class="returned-research"><h5>Mailing research · ${esc(labels[research.status]||research.status)}</h5>
      <p class="subtle">${research.finished_at?'Checked '+esc(new Date(research.finished_at).toLocaleString()):'Runs automatically after upload.'} ${research.attempts>=5&&['PARTIAL','FAILED','RUNNING'].includes(research.status)?'Automatic retries exhausted; administrator attention needed.':''}</p>
      <p>Research leads require appraiser verification. Postal validation: not performed. No CRS changes were made.</p>
      ${research.error?`<p class="status warn">${esc(research.error)}</p>`:''}
      ${crs?`<div class="returned-research-current"><strong>Current imported CRS mailing</strong><div>${esc(crs.recipient||result.account?.owner||'Recipient unavailable')}</div><pre>${esc(addressText(crs)||'No imported CRS mailing address')}</pre><p class="subtle">Source as of ${esc(crs.source_as_of_date||'unknown')} · Imported ${esc(crs.imported_at||'unknown')} · Exact account number; verify envelope recipient.</p></div>`:''}
      ${(result.leads||[]).map(lead=>`<article class="returned-research-lead"><strong>${esc(lead.source)}</strong><div>${esc(lead.recipient||'')}${lead.account?' · Account '+esc(lead.account):''}${lead.taxpayer_id?' · Taxpayer '+esc(lead.taxpayer_id):''}</div><pre>${esc(lead.raw_address||addressText(lead.address)||'Address unavailable')}</pre>
      <p>${esc(lead.relationship||(lead.reasons||[]).join('; '))}</p><p class="subtle">Account identity: ${esc(({HIGH:'High — existing CAD matching rules',LOW:'Low — shared recipient/owner name only',UNVERIFIED_OCR:'Unverified scan reading',UNVERIFIED:'Unverified'})[lead.identity_confidence]||lead.identity_confidence)} · Source as of ${esc(lead.source_date||'unknown')} · Retrieved/imported ${esc(lead.retrieved_at||'unknown')}${lead.address&&!lead.address.complete?' · INCOMPLETE: '+esc((lead.address.incomplete_reasons||[]).join('; ')):''}${lead.same_as_current?' · Same as current CRS address':''}</p>${lead.account_match?`<details><summary>Account-match evidence</summary>${(lead.account_match.locations||[]).slice(0,50).map(location=>`<p>${esc(location.situs||'Situs unavailable')} · ${esc(location.confidence)} · Account ${esc(location.account||'unmatched')}<br>${esc(location.reason||'')}</p>`).join('')}</details>`:''}</article>`).join('')}
      ${web.status?`<div><strong>Internet research · ${esc(({NOT_CONFIGURED:'Not configured',NOT_NEEDED:'Internal lead available',INSUFFICIENT_IDENTITY:'Business name needed',FAILED:'Retry needed',NO_RELIABLE_SOURCE:'No reliable source found',LEADS_FOUND:'Possible business contact leads'})[web.status]||web.status)}</strong><pre>${esc(summary)}</pre><p class="subtle">Web findings are unverified leads. Business locations and registered/legal offices are not proof of a mailing address. Checked ${esc(web.checked_at||'not searched')}.</p>${sources.length?`<ul>${sources.map((source,index)=>`<li>Source ${index+1}: <a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title)}</a></li>`).join('')}</ul>`:''}</div>`:''}
      ${(result.warnings||[]).map(w=>`<p class="subtle">${esc(w)}</p>`).join('')}
      ${research.status==='COMPLETE'&&!(result.leads||[]).length&&web.status!=='LEADS_FOUND'?'<p>No reliable alternative mailing lead was found in the sources checked.</p>':''}
    </section>`;
  }
  function card(doc){
    const status=({PENDING:'Needs review',REVIEWED:'Reviewed',DISMISSED:'Dismissed'})[doc.review_status]||doc.review_status;
    const reading=({READ:'Text read — verify',PARTIAL:'Partially read',UNAVAILABLE:'Manual reading needed',UNREADABLE:'No readable text'})[doc.ocr_status]||doc.ocr_status;
    return `<article class="intel-support returned-mail-scan" id="return-doc-${esc(doc.id)}"><h4>${esc(doc.filename)} · ${esc(status)}</h4>
      <p class="subtle">Imported ${esc(new Date(doc.uploaded_at).toLocaleString())} · Text reading: ${esc(reading)}${doc.reviewed_at?' · Reviewed '+esc(new Date(doc.reviewed_at).toLocaleString()):''}</p>
      ${doc.account_mismatch?'<p class="status warn">A printed account number differs from the uploaded account. Verify the envelope before acting.</p>':''}
      <p>${esc((doc.ocr_warnings||[]).join(' '))}</p><button type="button" class="secondary" data-return-download="${esc(doc.id)}">Download original scan</button> <button type="button" class="secondary" data-return-text="${esc(doc.id)}">Show scanned text</button><pre class="hidden" data-return-ocr="${esc(doc.id)}" style="white-space:pre-wrap;max-height:240px;overflow:auto"></pre>
      <div data-return-research="${esc(doc.id)}">${researchHtml(doc.research)}</div><button type="button" class="secondary" data-return-research-refresh="${esc(doc.id)}">Refresh research</button>
      <p class="subtle">Reading suggestions require verification. A forwarding address is a research lead, not a confirmed correction.</p>
      <fieldset id="return-fields-${esc(doc.id)}" ${canEdit(doc)?'':'disabled'}>
      <div class="layout-3"><label>Return reason<select data-return-field="return_reason">${Object.entries(reasons).map(([k,v])=>`<option value="${esc(k)}" ${k===(doc.return_reason||doc.suggested_reason)?'selected':''}>${esc(v)}</option>`).join('')}</select></label>
      <label>Return date, if known<input type="date" data-return-field="return_date" value="${esc(doc.return_date||'')}"></label>
      <label>Scan review<select data-return-field="review_status"><option value="REVIEWED">Reviewed</option><option value="DISMISSED" ${doc.review_status==='DISMISSED'?'selected':''}>Dismiss / wrong account</option></select></label></div>
      <label>Forwarding address shown on scan — verify before any CAD change<textarea rows="3" data-return-field="forwarding_address">${esc(doc.review_status==='PENDING'?(doc.suggested_forwarding||''):(doc.forwarding_address||''))}</textarea></label>
      <label>Review notes<textarea rows="2" data-return-field="review_notes">${esc(doc.review_notes||'')}</textarea></label><button type="button" data-return-save="${esc(doc.id)}">Save scan review</button></fieldset>
      <p class="subtle" data-return-access="${esc(doc.id)}">${canEdit(doc)?'':'Start review on this scan’s returned-mail queue item to edit it. Completed scans retain their history.'}</p>
      <p role="status" data-return-status="${esc(doc.id)}"></p></article>`;
  }
  async function open(item,more=false){
    const token=++seq,identity=cfg.identity();owner=identity;
    if(!more){docs=[];dirty.clear();next=null;el('returned-evidence-list').replaceChildren();}
    el('returned-evidence').classList.toggle('hidden',item.signal_type!=='mailing_address_change');
    if(item.signal_type!=='mailing_address_change')return;
    el('returned-evidence-status').textContent='Loading returned-mail scans…';
    try{
      const data=await cfg.api('returned-mail/list',{source_table:item.source_table,item_id:item.id,offset:more?next||0:0});
      if(token!==seq||identity!==cfg.identity()||cfg.selected()?.id!==item.id)return;
      reasons=data.reasons;docs.push(...data.documents);next=data.next_offset;
      for(const doc of data.documents)el('returned-evidence-list').insertAdjacentHTML('beforeend',card(doc));
      el('returned-evidence-status').textContent=docs.length?`${docs.length} scans loaded for account ${item.matched_account_number}. Each file is evidence; file count is not a confirmed count of returned envelopes.`:'No returned-mail scans for this account.';
      el('returned-evidence-more').classList.toggle('hidden',next==null);access();
    }catch(error){if(token===seq)el('returned-evidence-status').textContent=error.message;}
  }
  async function save(id){
    const doc=docs.find(d=>d.id===id);if(!doc||!canEdit(doc)||saving)return;
    const node=document.getElementById('return-doc-'+id),payload={document_id:id,version:doc.version};
    node.querySelectorAll('[data-return-field]').forEach(input=>payload[input.dataset.returnField]=input.value|| (input.dataset.returnField==='return_date'?null:''));
    const identity=cfg.identity();saving=true;access();
    try{
      const data=await cfg.api('returned-mail/review',payload);
      if(identity!==cfg.identity()||owner!==identity)return;
      Object.assign(doc,data.document);dirty.delete(id);node.outerHTML=card(doc);
      const status=document.querySelector(`[data-return-status="${id}"]`);if(status)status.textContent='Review saved. No CAD address was changed.';
    }catch(error){const status=node.querySelector('[data-return-status]');if(status)status.textContent=error.message;}
    finally{saving=false;access();}
  }
  async function importFolder(){
    const files=Array.from(el('returned-folder').files||[]).filter(f=>!f.name.startsWith('.')&&/\.(pdf|png|jpe?g)$/i.test(f.name));
    if(!files.length||files.length>1000){el('returned-import-status').textContent='Choose 1–1,000 PDF/JPEG/PNG scans in account-number folders.';return;}
    const identity=cfg.identity();let added=0,duplicates=0,failed=0;
    el('returned-import').disabled=true;el('returned-folder').disabled=true;el('returned-import-errors').replaceChildren();
    for(let i=0;i<files.length;i++){
      if(identity!==cfg.identity()){el('returned-import-status').textContent='Stopped because the signed-in account changed. Re-select the folder after signing in.';break;}
      const file=files[i],path=file.webkitRelativePath||file.name;
      el('returned-import-status').textContent=`Importing ${i+1}/${files.length}: ${path}`;
      try{
        accountFromPath(path);if(file.size>12*1024*1024)throw Error('Scan exceeds 12 MB.');
        const encoded=String(await cfg.read(file)).split(',')[1];
        if(identity!==cfg.identity())throw Error('Signed-in account changed; import stopped.');
        const result=await cfg.api('returned-mail/import',{relative_path:path,file_base64:encoded});
        if(identity!==cfg.identity())break;
        if(result.duplicate)duplicates++;else added++;
      }catch(error){failed++;const line=document.createElement('li');line.textContent=path+': '+error.message;el('returned-import-errors').appendChild(line);}
      el('returned-import-status').textContent=`${added} imported · ${duplicates} duplicates skipped · ${failed} failed (${i+1}/${files.length}). Failed files can be retried by importing the folder again.`;
    }
    el('returned-import').disabled=false;el('returned-folder').disabled=false;
    if(identity===cfg.identity())try{await cfg.refresh();}catch{el('returned-import-status').textContent+=' Refresh the mailing queue to see saved imports.';}
  }
  function configure(options){
    cfg=options;
    el('returned-import').addEventListener('click',importFolder);
    el('returned-evidence-more').addEventListener('click',()=>{if(cfg.selected())open(cfg.selected(),true);});
    el('returned-evidence-list').addEventListener('input',event=>{const card=event.target.closest('[id^="return-doc-"]');if(card)dirty.add(card.id.slice('return-doc-'.length));});
    el('returned-evidence-list').addEventListener('click',async event=>{
      const button=event.target.closest('button');if(!button)return;
      if(button.dataset.returnResearchRefresh){
        const id=button.dataset.returnResearchRefresh,identity=cfg.identity(),token=seq;
        button.disabled=true;
        try{
          const data=await cfg.api('returned-mail/research',{document_id:id});
          if(identity!==cfg.identity()||token!==seq)return;
          const doc=docs.find(d=>d.id===id);if(doc)doc.research=data.research;
          const output=document.querySelector(`[data-return-research="${id}"]`);if(output)output.innerHTML=researchHtml(data.research);
        }catch(error){if(identity===cfg.identity())el('returned-evidence-status').textContent=error.message;}
        finally{button.disabled=false;}
        return;
      }
      const id=button.dataset.returnSave||button.dataset.returnDownload||button.dataset.returnText;
      if(button.dataset.returnSave)return save(id);
      try{
        if(button.dataset.returnDownload)return await cfg.download(id);
        if(button.dataset.returnText){const identity=cfg.identity();const data=await cfg.api('returned-mail/detail',{document_id:id});if(identity!==cfg.identity())return;const output=document.querySelector(`[data-return-ocr="${id}"]`);if(output){output.textContent=data.document.ocr_text||'No text could be read. Review the original scan.';output.classList.remove('hidden');}}
      }catch(error){el('returned-evidence-status').textContent=error.message;}
    });
  }
  root.ReturnedMail={configure,open,access,isDirty:()=>dirty.size>0,isSaving:()=>saving,reset:()=>{seq++;dirty.clear();docs=[];owner=null;},accountFromPath,card,researchHtml};
})(globalThis);
