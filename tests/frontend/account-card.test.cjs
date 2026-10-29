const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('frontend/bpp-account-card.js','utf8').replace('return {load,reset,eligible,auto,access,','return {save,finish,_setup:(s)=>Object.assign(state,s),load,reset,eligible,auto,access,');
function fixture(fail=false){
  const status={},reserve={},note={},fields=[{dataset:{cardField:'business_name'},value:'Business <One>'}];
  const root={addEventListener(){},querySelectorAll:s=>s==='[data-card-field]'?fields:[],querySelector:s=>s==='[data-card-status]'?status:s==='[data-card-reserve]'?reserve:note};
  const popup={closed:false,document:{body:{},write(html){this.html=html;},close(){}},close(){this.closed=true;}};
  const context={document:{getElementById:()=>root},window:{open:()=>popup},console};
  vm.createContext(context);vm.runInContext(source+';globalThis.api=BppAccountCard;',context);
  const item={id:'1',source_table:'bpp_intelligence_items',work_claim:{mine:true},status:'NEW'};
  let saves=0;
  context.api._setup({seq:1,dirty:true,card:{version:0,sections:[],data:{},district_name:'LCAD'},options:{selected:()=>item,send:async()=>{saves++;if(fail)throw Error('Save failed');return {version:1,saved_at:'2026-10-28T12:00:00Z'};}}});
  return {api:context.api,popup,status,saves:()=>saves};
}
test('finish saves first and exports a saved, escaped worksheet',async()=>{
  const f=fixture();await f.api.finish();assert.equal(f.saves(),1);assert.equal(f.api.isDirty(),false);
  assert.match(f.popup.document.html,/Saved /);assert.match(f.popup.document.html,/Business &lt;One&gt;/);assert.match(f.popup.document.html,/Save as PDF/);assert.equal(f.popup.closed,false);
});
test('failed save preserves dirty edits and closes export window',async()=>{
  const f=fixture(true);await f.api.finish();assert.equal(f.api.isDirty(),true);assert.equal(f.popup.closed,true);assert.equal(f.popup.document.html,undefined);assert.equal(f.status.textContent,'Save failed');
});
