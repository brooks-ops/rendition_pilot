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
