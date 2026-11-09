const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync('frontend/index.html','utf8');
function fn(name){const start=source.indexOf('    function '+name+'(');assert.ok(start>=0);return source.slice(start,source.indexOf('\n    }',start)+6);}
const ctx={cadHtml:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),cadDate:v=>v||'unknown'};
vm.createContext(ctx);vm.runInContext(fn('isReturnedMailItem')+fn('returnedMailBadgeHtml')+fn('mailingComparisonHtml'),ctx);
test('mailing evidence separates CRS, source dates, identity and validation and escapes all imported text',()=>{
 const hostile='<img src=x onerror=alert(1)>';
 const evidence={comparison_version:2,comparison_status:'DISCREPANCY',identity_confidence:'HIGH',discrepancy_reasons:['unit differs'],
  crs:{recipient:hostile,line1:'PO Box 500',source:'imported_crs_export',imported_at:'2026-11-02',source_as_of_date:'2026-10-01',complete:true,field_sources:{mailing_city:{imported_at:'2026-10-01'}}},
  comptroller:{recipient:'Acme',line1:'200 Main St',retrieved_at:'2026-11-02',complete:true},
  account_match:{locations:[{situs:hostile,reason:hostile,account:'P1',confidence:'HIGH',signals:{owner:hostile}}]}};
 const html=ctx.mailingComparisonHtml({raw:{evidence}});
 assert.match(html,/Current imported CRS mailing/);assert.match(html,/Latest Comptroller mailing/);
 assert.match(html,/source effective date unknown/);assert.match(html,/Account identity confidence: HIGH/);
 assert.match(html,/Postal validation: not performed/);assert.match(html,/Field source dates/);
 assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
});
test('legacy observations are never presented as current CRS mailing',()=>{
 const html=ctx.mailingComparisonHtml({mailing_address_current:'A',mailing_address_observed:'B',raw:{}});
 assert.match(html,/not the current CRS address/);assert.match(html,/Postal validation has not been performed/);
});
test('missing CRS data has an explicit gap and no approved-correction claim',()=>{
 const html=ctx.mailingComparisonHtml({raw:{evidence:{comparison_version:2,comparison_status:'MISSING_CRS_MAILING',discrepancy_reasons:['No CRS mailing address'],crs:{},comptroller:{}}}});
 assert.match(html,/No address supplied/);assert.match(html,/MISSING CRS MAILING/);assert.match(html,/not an approved correction/);
});

test('returned scans have a visible source badge and account wording for flat PDFs',()=>{
 const item={matched_account_number:'P105330',raw:{source:'returned_mail_scan',evidence:{}}};
 assert.match(ctx.returnedMailBadgeHtml(item),/>Returned mail</);
 const html=ctx.mailingComparisonHtml(item);
 assert.match(html,/CAD account: P105330/);
 assert.doesNotMatch(html,/Account folder|Legacy Comptroller/);
 assert.equal(ctx.returnedMailBadgeHtml({raw:{source:'crs_comptroller_mailing_comparison'}}),'');
 assert.equal(ctx.isReturnedMailItem({source:'returned_mail_scan'}),true);
});
test('queue labels returned scans without presenting them as legacy address changes',()=>{
 Object.assign(ctx,{intelWorkKey:()=>'',intelWorkLabel:()=>'',intelResolutionLabel:()=>''});
 vm.runInContext(fn('intelligenceTableHtml'),ctx);
 const html=ctx.intelligenceTableHtml([{id:'scan',source_table:'bpp_intelligence_items',business_name:'Example',signal_type:'mailing_address_change',mailing_address_current:'100 Main St',matched_account_number:'P105330',raw:{source:'returned_mail_scan'},status:'NEW',priority:'HIGH'}]);
 assert.match(html,/returned-mail-source-badge/);
 assert.match(html,/Scanned returned envelope/);
 assert.match(html,/100 Main St/);
 assert.doesNotMatch(html,/Legacy mailing history|Legacy source observation|Address unavailable/);
});
