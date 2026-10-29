const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
const html=fs.readFileSync('frontend/index.html','utf8');
function fn(name){const start=html.indexOf('    function '+name+'(');assert.ok(start>=0,name);const end=html.indexOf('\n    function ',start+1);return html.slice(start,end);}
const fields={};const context={$:id=>fields[id]||(fields[id]={}),console};vm.createContext(context);
for(const name of ['formatText','formatMoney','formatPercent','prettifyPath','prettifyConfidence','renderKvRows','renderDetails','renderAgentReview','renderCandidates','renderTable'])vm.runInContext(fn(name),context);
// cadHtml lives beside a compact helper; extract only that declaration.
vm.runInContext(html.match(/function cadHtml\(value\) \{[^\n]+/)[0],context);
const hostile='<img src=x onerror="window.injected=true">';
test('extracted document metadata, notes, reasoning and candidates are escaped as text',()=>{
 context.renderDetails({metadata:{owner_name:hostile},manual_override:{notes:hostile},structured_extraction:{debug:{document_ai_error:hostile},ocr_reconciliation:{provider_summaries:{[hostile]:{text_chars:hostile}}}}});
 context.renderAgentReview({agent_review:{reasoning:hostile,recommended_values:{selected_source:hostile}}});
 context.renderCandidates({selected_candidate:{label:hostile},value_candidates:[{label:hostile,source:hostile,evidence_text:hostile}]});
 for(const id of ['details-section','agent-review-section','candidate-section']){assert.ok(!fields[id].innerHTML.includes('<img'));assert.ok(fields[id].innerHTML.includes('&lt;img'));}
});
test('imported table names and values do not create HTML elements',()=>{
 context.renderTable('head','body',[{[hostile]:hostile}]);assert.ok(!fields.head.innerHTML.includes('<img'));assert.ok(!fields.body.innerHTML.includes('<img'));
 assert.ok(fields.body.innerHTML.includes('&lt;img'));
});
test('invalid currency and percentages never pass markup into HTML',()=>{
 assert.equal(context.formatMoney(hostile),'-');assert.equal(context.formatPercent(hostile),'-');
});
