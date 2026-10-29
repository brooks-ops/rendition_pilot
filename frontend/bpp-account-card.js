/* LCAD field worksheet; drafts remain on the existing intelligence item. */
const BppAccountCard = (() => {
  let state={seq:0,card:null,dirty:false,saving:false,options:null};
  const root=()=>document.getElementById('intel-account-card');
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const segmentFields=[['valuation_method','Valuation method'],['type_code','Type code'],['flat_value','Flat value'],['appraisal_method','Appraisal method']];
  const key=item=>item?`${item.source_table}:${item.id}`:'';
  function eligible(item) { return item?.signal_type==='new_business'||(item?.signal_type==='county_clerk_dba'&&(item.raw?.filing_type==='ASSUMED NAME'||item.resolution==='NEW_ACCOUNT_NEEDED')); }
  function auto(item) { return item?.signal_type==='new_business'||item?.resolution==='NEW_ACCOUNT_NEEDED'||(item?.signal_type==='county_clerk_dba'&&item.classification==='DBA_POTENTIAL_NEW_BUSINESS'); }
  function reset() { state={...state,seq:state.seq+1,card:null,dirty:false,saving:false};root().replaceChildren(); }
  function read() {
    const data={};root().querySelectorAll('[data-card-field]').forEach(el=>data[el.dataset.cardField]=el.value);
    data.segments=[...root().querySelectorAll('[data-card-segment]')].map(row=>Object.fromEntries(segmentFields.map(([name])=>[name,row.querySelector(`[data-segment-field="${name}"]`).value])));
    return data;
  }
  function total(data) {
    const amounts=data.segments.map(r=>r.flat_value).filter(v=>v.trim()!=='');
    if(!amounts.length)return null;
    if(amounts.some(v=>!Number.isFinite(Number(v))))return 'Invalid value';
    return (amounts.reduce((sum,v)=>sum+Math.round(Number(v)*100),0)/100).toFixed(2);
  }
  function totalLabel(data) { const n=total(data);return n===null?'Not entered':n==='Invalid value'?n:Number(n).toLocaleString('en-US',{style:'currency',currency:'USD'}); }
  function message(text,error=false) { const el=root().querySelector('[data-card-status]');if(el){el.textContent=text;el.className='status '+(error?'error':'');} }
  function access() {
    if(!state.card)return;
    const item=state.options.selected(),mine=!!item?.work_claim?.mine,closed=['RESOLVED','DISMISSED'].includes(item?.status);
    root().querySelectorAll('input,textarea,select,[data-card-save],[data-card-finish],[data-card-defer],[data-card-copy],[data-card-today],[data-card-add],[data-card-remove]').forEach(el=>el.disabled=!mine||closed||state.saving);
    const reserve=root().querySelector('[data-card-reserve]');reserve.hidden=mine||closed;reserve.disabled=!!item?.work_claim||state.saving;
    root().querySelector('[data-card-edit-note]').textContent=closed?'Completed review · worksheet is read-only.':mine?'Editing your reserved account.':item?.work_claim?'Read-only while another appraiser works on this account.':'Start editing to reserve this account.';
  }
  function render() {
    const c=state.card,d=c.data;
    root().innerHTML=`<div class="account-card-heading"><div><h3>New account card</h3><p class="subtle">${esc(c.district_name)} · Field review worksheet</p></div><div class="button-row"><button type="button" data-card-reserve>Start editing</button><button type="button" class="secondary" data-card-print>Print / save PDF</button><button type="button" class="secondary" data-card-save>Save draft</button><button type="button" data-card-finish>Finish &amp; print / PDF</button><button type="button" class="secondary" data-card-defer>Finish — print later</button></div></div>
      <p class="subtle" data-card-edit-note></p><p class="subtle">Review the prefilled details, add what you know, then finish to save, mark complete, and open the print / PDF view. Blank fields are allowed. Finishing completes this review; it does not create an official CAD account.</p>
      ${c.existing_accounts?.length?`<div class="status warn">BPP accounts already found: ${esc(c.existing_accounts.join(', '))}. Verify before creating another account.</div>`:''}
      ${c.sections.map(section=>`${section.title==='Property and codes'?'<details class="account-card-section"><summary>Property and codes · optional</summary>':`<section class="account-card-section"><h4>${esc(section.title)}</h4>`}${section.title==='Mailing'?'<div class="button-row"><button type="button" class="secondary" data-card-copy>Use business location for mailing</button></div><p class="subtle">Use only when you have confirmed the mailing address is the same.</p>':''}<div class="account-card-grid">${section.fields.map(f=>`<div class="field ${f.type==='textarea'?'account-card-wide':''}"><label for="account-card-${f.name}">${esc(f.label)}</label>${f.name==='new_value'?`<select id="account-card-${f.name}" data-card-field="${f.name}"><option value="">Not entered</option>${['Y','N'].map(v=>`<option value="${v}" ${d[f.name]===v?'selected':''}>${v==='Y'?'Yes':'No'}</option>`).join('')}</select>`:f.type==='textarea'?`<textarea id="account-card-${f.name}" data-card-field="${f.name}" rows="3" maxlength="4000">${esc(d[f.name])}</textarea>`:`<input id="account-card-${f.name}" data-card-field="${f.name}" type="${f.type}" ${f.type==='number'?'min="1900" max="2200" step="1"':''} maxlength="500" value="${esc(d[f.name])}" ${({contact_name:'name',contact_email:'email',contact_phone:'tel'})[f.name]?`autocomplete="${({contact_name:'name',contact_email:'email',contact_phone:'tel'})[f.name]}"`:''}>`}${f.name==='last_inspected'?'<button type="button" class="secondary" data-card-today>Use today</button>':''}</div>`).join('')}</div>${section.title==='Property and codes'?'</details>':'</section>'}`).join('')}
      <section class="account-card-section"><h4>Valuation segments</h4><div class="account-card-table"><table><thead><tr><th>Segment</th>${segmentFields.map(([,label])=>`<th>${label}</th>`).join('')}<th></th></tr></thead><tbody>${d.segments.map((row,i)=>`<tr data-card-segment><td>${i+1}</td>${segmentFields.map(([name,label])=>`<td><input aria-label="Segment ${i+1} ${label}" data-segment-field="${name}" ${name==='flat_value'?'type="number" step="0.01"':'type="text" maxlength="80"'} value="${esc(row[name])}"></td>`).join('')}<td><button type="button" class="secondary" data-card-remove="${i}" aria-label="Remove segment ${i+1}">×</button></td></tr>`).join('')}</tbody></table></div><div class="account-card-total"><button type="button" class="secondary" data-card-add>Add segment</button><strong>Total value: <span data-card-total>${totalLabel(d)}</span></strong></div></section>
      ${c.checks?.length?`<details class="intel-support"><summary>Source and property checks (${c.checks.length})</summary><div>${c.checks.map(check=>`<p>${esc(check)}</p>`).join('')}</div></details>`:''}
      <div class="button-row"><button type="button" data-card-finish>Finish &amp; print / PDF</button><button type="button" class="secondary" data-card-defer>Finish — print later</button><button type="button" class="secondary" data-card-save>Save draft</button><button type="button" class="secondary" data-card-print>Print / save PDF</button></div><div data-card-status role="status" class="status">${state.dirty?'Unsaved changes.':c.saved_at?'Saved '+esc(new Date(c.saved_at).toLocaleString())+'.':'Draft · not yet saved.'}</div>`;
    access();
  }
  async function load(item,options) {
    const seq=++state.seq;state.options=options;state.key=key(item);state.card=null;state.dirty=false;
    root().classList.remove('hidden');root().textContent='Loading new-account card…';
    try {
      const result=await options.send('/api/admin/intelligence/account-card',{source_table:item.source_table,item_id:item.id});
      if(seq!==state.seq||key(options.selected())!==key(item))return;
      state.card=result.card;render();
    } catch(error) { if(seq===state.seq){root().textContent='Account card unavailable: '+error.message;const retry=document.createElement('button');retry.type='button';retry.textContent='Retry card';retry.onclick=()=>load(item,options);root().appendChild(retry);} }
  }
  async function save() {
    if(!state.card||state.saving)return false;
    const item=state.options.selected(),seq=state.seq,data=read();state.saving=true;access();message('Saving card…');
    try {
      const result=await state.options.send('/api/admin/intelligence/account-card/save',{source_table:item.source_table,item_id:item.id,version:state.card.version,data});
      if(seq!==state.seq)return false;
      state.card={...state.card,data,version:result.version,saved_at:result.saved_at};state.dirty=false;message('Account card saved. Ready to print or save as PDF.');return true;
    } catch(error) { if(seq===state.seq)message(error.message,true);return false; }
    finally { if(seq===state.seq){state.saving=false;access();} }
  }
  function print(existingWindow) {
    if(!state.card)return;const c=state.card,d=read(),win=existingWindow||window.open('','_blank');if(!win){message('Allow pop-ups to print this card.',true);return;}
    win.document.write(printHtml(c,d,state.dirty));win.document.close();
  }
  function printHtml(c,d,dirty=false) {
    const field=(label,value)=>`<div class="field"><b>${esc(label)}</b><div>${esc(value)||'—'}</div></div>`;
    return `<!doctype html><html><head><title>${esc(d.business_name)} — New account card</title><style>body{font:12px Arial;color:#182b27;margin:28px}h1{font-size:19px}h2{font-size:14px;border-bottom:1px solid #aaa;padding-bottom:5px;margin:18px 0 9px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.field{break-inside:avoid;white-space:pre-wrap;overflow-wrap:anywhere}.field b{font-size:10px;color:#555}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:7px;text-align:left}@media print{button,.print-help{display:none}body{margin:0}.section{break-inside:avoid}thead{display:table-header-group}}@page{size:letter;margin:.45in}</style></head><body><button onclick="window.print()">Print / save PDF</button><p class="print-help">Choose a printer, or select Save as PDF in the print dialog.</p><h1>${esc(c.district_name)}</h1><p>Personal property field review worksheet · ${dirty?'Unsaved draft':c.saved_at?'Saved '+esc(new Date(c.saved_at).toLocaleString()):'Draft'}</p>${c.sections.map(s=>`<section class="section"><h2>${esc(s.title)}</h2><div class="grid">${s.fields.map(f=>field(f.label,d[f.name])).join('')}</div></section>`).join('')}<section class="section"><h2>Valuation segments</h2><table><thead><tr><th>Segment</th>${segmentFields.map(([,label])=>`<th>${label}</th>`).join('')}</tr></thead><tbody>${d.segments.map((r,i)=>`<tr><td>${i+1}</td>${segmentFields.map(([name])=>`<td>${esc(r[name])||'—'}</td>`).join('')}</tr>`).join('')}</tbody></table><p><b>Total value: ${totalLabel(d)}</b></p></section></body></html>`;
  }
  function batchPrint(cards) {
    if(!cards.length)return;
    const win=window.open('','_blank');if(!win)throw Error('Allow pop-ups to print the selected cards.');
    const documents=cards.map(c=>printHtml(c,c.data));
    const head=documents[0].split('<body>')[0].replace(/<title>.*?<\/title>/,'<title>Completed account cards</title>');
    const bodies=documents.map(html=>'<article class="batch-card">'+html.split('<body>')[1].split('</body>')[0]+'</article>').join('');
    win.document.write(head+'<style>@media print{.batch-card{break-before:page}.batch-card:first-child{break-before:auto}}</style><body>'+bodies+'</body></html>');win.document.close();
  }
  async function finish(defer=false) {
    if(!state.card||state.saving)return;
    const seq=state.seq,options=state.options,item=options.selected(),win=defer?null:window.open('','_blank');
    if(!defer&&!win){message('Allow pop-ups to finish and print this card.',true);return;}
    if(win)win.document.body.textContent='Saving account card…';
    if(!await save()){win?.close();return;}
    if(seq!==state.seq||win?.closed){win?.close();return;}
    if(win)print(win);
    try { await options.complete(item,defer); }
    catch(error) { if(seq===state.seq)message('Card saved, but the review was not completed: '+error.message,true); }
  }
  document.getElementById('intel-account-card').addEventListener('input',()=>{if(!state.card)return;state.dirty=true;root().querySelector('[data-card-total]').textContent=totalLabel(read());message('Unsaved changes.');});
  document.getElementById('intel-account-card').addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button||!state.card)return;
    if(button.hasAttribute('data-card-save'))return save();
    if(button.hasAttribute('data-card-print'))return print();
    if(button.hasAttribute('data-card-finish'))return finish();
    if(button.hasAttribute('data-card-defer'))return finish(true);
    if(button.hasAttribute('data-card-copy')||button.hasAttribute('data-card-today')){
      const data=read();
      if(button.hasAttribute('data-card-copy')){
        const pairs=[['mailing_address','situs_address'],['mailing_city','situs_city'],['mailing_state','situs_state'],['mailing_zip','situs_zip']];
        if(pairs.some(([to,from])=>data[to]&&data[to]!==data[from])&&!window.confirm('Replace the current mailing details with the business location?'))return;
        pairs.forEach(([to,from])=>data[to]=data[from]);
      }else{const now=new Date();data.last_inspected=[now.getFullYear(),String(now.getMonth()+1).padStart(2,'0'),String(now.getDate()).padStart(2,'0')].join('-');}
      state.card.data=data;state.dirty=true;render();message('Details filled in. Review and save your changes.');return;
    }
    if(button.hasAttribute('data-card-reserve'))return state.options.reserve();
    if(button.hasAttribute('data-card-add')||button.hasAttribute('data-card-remove')){
      const data=read();if(button.hasAttribute('data-card-add')){if(data.segments.length>=20){message('Use at most 20 segments.',true);return;}data.segments.push(Object.fromEntries(segmentFields.map(([name])=>[name,''])));}else data.segments.splice(Number(button.dataset.cardRemove),1);
      state.card.data=data;state.dirty=true;render();
    }
  });
  return {load,reset,eligible,auto,access,batchPrint,isDirty:()=>state.dirty,isSaving:()=>state.saving};
})();
