const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync('frontend/index.html','utf8');
function fn(name){const start=source.indexOf('    function '+name+'(');assert.ok(start>=0);return source.slice(start,source.indexOf('\n    }',start)+6);}
test('Save & Next follows current order, skips other appraisers and completed accounts',()=>{
 const ctx={};vm.createContext(ctx);vm.runInContext(fn('nextReview'),ctx);
 const row=(id,status='NEW',work_claim=null)=>({id,status,source_table:'bpp_intelligence_items',work_claim});
 const previous=[row('a'),row('b'),row('c'),row('d')];
 assert.equal(ctx.nextReview([row('b','NEW',{mine:false}),row('c','RESOLVED'),row('d')],previous[0],previous).id,'d');
 assert.equal(ctx.nextReview([],previous[0],previous),undefined);
 assert.equal(ctx.nextReview([row('a'),row('b')],previous[3],previous).id,'a');
});
test('filter preferences are scoped per user and district and survive storage failures',()=>{
 const memory=new Map(),els={};const ids=['intel-filter-signal','intel-filter-status','intel-filter-confidence','intel-filter-city','mailing-filter-status','mailing-filter-confidence'];
 ids.forEach(id=>els[id]={value:'',tagName:id.endsWith('city')?'INPUT':'SELECT',options:[{value:''},{value:'HIGH'},{value:'new_business'}]});
 let owner='district1:user1';const folders={intel:'active',mailing:'active'};
 const ctx={$:id=>els[id],queuePreferenceOwner:null,queueFilterIds:ids,queueFolders:folders,RenditionSession:{identity:()=>owner},localStorage:{getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v)},setQueueFolder:(p,f)=>folders[p]=f};
 vm.createContext(ctx);vm.runInContext(fn('ensureQueuePreferences')+'\n'+fn('saveQueuePreferences'),ctx);
 ctx.ensureQueuePreferences();els['intel-filter-city'].value='LUBBOCK';els['mailing-filter-confidence'].value='HIGH';ctx.saveQueuePreferences();
 owner='district2:user1';ctx.ensureQueuePreferences();assert.equal(els['intel-filter-city'].value,'');
 owner='district1:user1';ctx.ensureQueuePreferences();assert.equal(els['intel-filter-city'].value,'LUBBOCK');assert.equal(els['mailing-filter-confidence'].value,'HIGH');
 ctx.localStorage.setItem=()=>{throw Error('unavailable')};assert.doesNotThrow(()=>ctx.saveQueuePreferences());
});
test('intelligence totals follow signal and filters from the same queue response',async()=>{
 const els={};const el=id=>els[id]??=( {value:'',textContent:'',classList:{toggle(){},remove(){}}});
 el('intel-filter-signal').value='sales_tax_inactive';el('intel-filter-confidence').value='HIGH';el('intel-filter-city').value='LUBBOCK';
 const requests=[];const ctx={$:el,canUseBpp:()=>true,ensureQueuePreferences(){},async loadAssignmentControls(){},saveQueuePreferences(){},queueRequests:{intel:0},queueFolders:{intel:'active'},appState:{auth:{access_token:'test'},intelligence:{}},renderFolderCounts(){},renderIntelligenceQueue(){},refreshCadHealth(){},fetchJson:async(path,options)=>{requests.push({path,payload:JSON.parse(options.body)});return path.endsWith('/queue')?{items:[],summary:{new:7,high_priority:3,needs_review:1,resolved:9}}:{configured:false};}};
 vm.createContext(ctx);const start=source.indexOf('    async function refreshIntelligenceQueue(');vm.runInContext(source.slice(start,source.indexOf('\n    }',start)+6),ctx);
 await ctx.refreshIntelligenceQueue();
 assert.equal(el('intel-summary-new').textContent,7);assert.equal(el('intel-summary-resolved').textContent,9);
 assert.equal(requests[0].payload.signal_type,'sales_tax_inactive');assert.equal(requests[0].payload.confidence,'HIGH');assert.equal(requests[0].payload.city,'LUBBOCK');
 assert.equal(requests.some(r=>r.path.endsWith('/summary')),false);
 el('intel-filter-signal').value='new_business';await ctx.refreshIntelligenceQueue();assert.equal(requests.filter(r=>r.path.endsWith('/queue')).at(-1).payload.signal_type,'new_business');
});

test('Save & Next skips work allocated to someone else even without a reservation',()=>{
 const ctx={appState:{auth:{user:{id:'me'}}}};vm.createContext(ctx);vm.runInContext(fn('nextReview'),ctx);
 const current={id:'a',source_table:'bpp_intelligence_items'};
 const other={id:'b',source_table:'bpp_intelligence_items',status:'NEW',raw:{assigned_to:'other'}};
 const mine={id:'c',source_table:'bpp_intelligence_items',status:'NEW',raw:{assigned_to:'me'}};
 assert.equal(ctx.nextReview([other,mine],current,[current,other,mine]).id,'c');
});
test('regular appraisers get a locked own-assignment filter instead of all-work options',()=>{
 const visibility=[];const select={value:'someone-else',closest:()=>({classList:{toggle:(name,hidden)=>visibility.push([name,hidden])}})};
 const ctx={$:()=>select,cadHtml:String};vm.createContext(ctx);vm.runInContext(fn('setAssignmentFilter'),ctx);
 ctx.setAssignmentFilter('intel',false,[{id:'someone-else',label:'Other appraiser'}]);
 assert.equal(select.value,'ME');assert.equal(select.disabled,true);assert.deepEqual(visibility,[['hidden',true]]);
 assert.ok(!select.innerHTML.includes('All appraisers'));assert.ok(!select.innerHTML.includes('Other appraiser'));
});
test('admin keeps all-work and appraiser filters with their current selection',()=>{
 const select={value:'someone-else',closest:()=>({classList:{toggle(){}}})};
 const ctx={$:()=>select,cadHtml:String};vm.createContext(ctx);vm.runInContext(fn('setAssignmentFilter'),ctx);
 ctx.setAssignmentFilter('mailing',true,[{id:'someone-else',label:'Other appraiser'}]);
 assert.equal(select.value,'someone-else');assert.equal(select.disabled,false);
 assert.ok(select.innerHTML.includes('All appraisers')&&select.innerHTML.includes('Unassigned')&&select.innerHTML.includes('Other appraiser'));
});
