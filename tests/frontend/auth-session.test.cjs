const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync('frontend/auth-session.js','utf8');
const now=2000000000000;
function session(overrides={}){return {cookie_session:true,access_token:'old',refresh_token:'refresh-old',expires_at:now/1000-1,user:{id:'u1'},district:{district_id:'d1'},...overrides};}
function setup(initial,fetcher,shared){
 const values=shared||new Map();if(initial)values.set('appraisalpilot_auth',JSON.stringify(initial));
 const handlers={};const localStorage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
 const context={localStorage,fetch:fetcher,Headers,URL,location:{origin:'https://app.test'},navigator:{},window:{addEventListener:(k,f)=>handlers[k]=f},document:{visibilityState:'visible',addEventListener(){}},setInterval(){},Date:class extends Date{static now(){return now;}},atob:s=>Buffer.from(s,'base64').toString()};
 vm.createContext(context);vm.runInContext(source+';this.api=RenditionSession',context);return {api:context.api,values,handlers,context};
}
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
test('concurrent expired requests renew once and update body and header',async()=>{
 let renewals=0,calls=0;
 const f=setup(session(),async(url,options)=>{if(url==='/api/auth/refresh'){renewals++;return json(session({access_token:'new',refresh_token:'refresh-new',expires_at:now/1000+3600}));}calls++;assert.equal(new Headers(options.headers).get('Authorization'),'Bearer new');assert.equal(JSON.parse(options.body).access_token,'new');return json({saved:true});});
 await Promise.all([1,2,3].map(()=>f.api.fetch('/api/save',{method:'POST',body:JSON.stringify({access_token:'old',value:'draft'})})));
 assert.equal(renewals,1);assert.equal(calls,3);assert.equal(f.api.read().refresh_token,undefined);assert.equal(JSON.parse(f.values.get('appraisalpilot_auth')).refresh_token,undefined);
});
test('a rejected unexpired token refreshes and retries exactly once',async()=>{
 let calls=0,refreshes=0;const f=setup(session({expires_at:now/1000+3600}),async(url)=>{if(url==='/api/auth/refresh'){refreshes++;return json(session({access_token:'new',expires_at:now/1000+3600}));}calls++;return json({},401);});
 const result=await f.api.fetch('/api/save');assert.equal(result.status,401);assert.equal(calls,2);assert.equal(refreshes,1);
});
test('transient refresh failures preserve the saved session and never replay writes',async()=>{
 let writes=0;const f=setup(session(),async url=>{if(url==='/api/auth/refresh')return json({detail:'Offline'},503);writes++;return json({});});
 await assert.rejects(f.api.fetch('/api/save'),/Offline/);assert.equal(f.api.read().refresh_token,'refresh-old');assert.equal(writes,0);
});
test('restoring session metadata preserves renewal credentials',()=>{
 const f=setup(session(),async()=>{});f.api.save({access_token:'old',user:{id:'u1'},district:{district_id:'d1'}});assert.equal(f.api.read().refresh_token,'refresh-old');
 f.api.save(session({user:{id:'u2'},refresh_token:undefined}));assert.equal(f.api.read().refresh_token,undefined);
});
test('signing out during refresh cannot resurrect a session',async()=>{
 let respond;const f=setup(session(),url=>url==='/api/auth/logout'?Promise.resolve(json({signed_out:true})):new Promise(r=>respond=r));const pending=f.api.ensure();await Promise.resolve();f.api.clear();respond(json(session({access_token:'new'})));
 await assert.rejects(pending,/session changed/i);assert.equal(f.api.read(),null);
});
test('two tabs serialize their HttpOnly cookie renewals without persisting access tokens',async()=>{
 const values=new Map();let queue=Promise.resolve(),refreshes=0;
 const locks={request:(_,fn)=>{const p=queue.then(fn);queue=p.catch(()=>{});return p;}};
 const fetcher=async()=>{refreshes++;return json(session({access_token:'new',refresh_token:'new-refresh',expires_at:now/1000+3600}));};
 const a=setup(session(),fetcher,values),b=setup(null,fetcher,values);a.context.navigator.locks=locks;b.context.navigator.locks=locks;
 await Promise.all([a.api.ensure(),b.api.ensure()]);assert.equal(refreshes,2);assert.equal(b.api.read().access_token,'new');assert.equal(JSON.parse(values.get('appraisalpilot_auth')).access_token,'cookie-session');
});
test('account switch in another tab blocks sending an old draft as the new user',async()=>{
 let requests=0;const f=setup(session({expires_at:now/1000+3600}),async()=>{requests++;return json({});});f.values.set('appraisalpilot_auth',JSON.stringify(session({user:{id:'u2'}})));
 await assert.rejects(f.api.fetch('/api/save',{body:'{"access_token":"old"}'}),/account changed/);assert.equal(requests,0);
});
test('legacy expired sessions get a clear sign-in message without raw JWT errors',async()=>{
 const f=setup(session({refresh_token:null,cookie_session:false}),async()=>{throw Error('Should not request');});await assert.rejects(f.api.fetch('/api/save'),/sign in once more/);
});
test('permission errors are never retried and binary exports keep their contents',async()=>{
 let calls=0;const f=setup(session({expires_at:now/1000+3600}),async url=>{calls++;return url==='/api/denied'?json({},403):new Response('PDF data',{headers:{'Content-Type':'application/pdf'}});});
 assert.equal((await f.api.fetch('/api/denied')).status,403);assert.equal(calls,1);assert.equal(await (await f.api.fetch('/api/export')).text(),'PDF data');
});

test('public bootstrap and sign-in remain usable with an expired legacy session',async()=>{
 let calls=0;const f=setup(session({refresh_token:null,cookie_session:false}),async()=>{calls++;return json({});});
 assert.equal((await f.api.fetch('/api/bootstrap')).status,200);assert.equal((await f.api.fetch('/api/auth/login',{method:'POST'})).status,200);assert.equal(calls,2);
});

test('a late restore response cannot replace a newer access token',()=>{
 const f=setup(session({access_token:'new',expires_at:now/1000+3600}),async()=>{});
 f.api.save({access_token:'old',expires_at:now/1000-1,user:{id:'u1'},district:{district_id:'d1'}});assert.equal(f.api.read().access_token,'new');assert.equal(f.api.read().expires_at,now/1000+3600);
});


test('credentials are removed from persistent storage immediately, including old sessions',()=>{
 const f=setup(session(),async()=>{});const saved=JSON.parse(f.values.get('appraisalpilot_auth'));
 assert.equal(saved.access_token,'cookie-session');assert.equal(saved.refresh_token,undefined);
 assert.equal(f.api.read().access_token,'old');
});
test('a reloaded tab exchanges the HttpOnly cookie before sending an API request',async()=>{
 let refreshes=0;const f=setup(session({access_token:'cookie-session',cookie_session:true,refresh_token:undefined,expires_at:now/1000+3600}),async(url,options)=>{
  if(url==='/api/auth/refresh'){refreshes++;assert.deepEqual(JSON.parse(options.body),{});assert.equal(options.credentials,'same-origin');return json(session({access_token:'renewed',cookie_session:true,refresh_token:undefined,expires_at:now/1000+3600}));}
  assert.equal(new Headers(options.headers).get('Authorization'),'Bearer renewed');return json({});
 });
 await f.api.fetch('/api/save');assert.equal(refreshes,1);assert.equal(f.api.read().refresh_token,undefined);
 const stored=JSON.parse(f.values.get('appraisalpilot_auth'));assert.equal(stored.access_token,'cookie-session');assert.equal(stored.refresh_token,undefined);
});
test('sign-out clears the server cookie and a following login waits for it',async()=>{
 let release;const calls=[];const f=setup(session(),async url=>{calls.push(url);if(url==='/api/auth/logout')return new Promise(r=>release=r);return json({});});
 const logout=f.api.clear();await new Promise(r=>setImmediate(r));const login=f.api.fetch('/api/auth/login',{method:'POST'});
 assert.deepEqual(calls,['/api/auth/logout']);release(json({signed_out:true}));await logout;await login;assert.deepEqual(calls,['/api/auth/logout','/api/auth/login']);
});

test('a valid legacy session migrates its renewal token to the protected cookie immediately',async()=>{
 let calls=0;const f=setup(session({cookie_session:false,expires_at:now/1000+3600}),async(url,options)=>{calls++;assert.equal(url,'/api/auth/refresh');assert.equal(JSON.parse(options.body).refresh_token,'refresh-old');return json(session({cookie_session:true,refresh_token:undefined,access_token:'migrated',expires_at:now/1000+3600}));});
 await f.api.ensure();assert.equal(calls,1);assert.equal(f.api.read().refresh_token,undefined);assert.equal(f.api.read().access_token,'migrated');
});
