/* Shared renewable Rendition Pilot session. Refresh credentials stay on this origin. */
const RenditionSession = (() => {
  const key='appraisalpilot_auth',listeners=new Set();
  let refreshing=null;
  const read=()=>{try{return JSON.parse(localStorage.getItem(key)||'null');}catch{return null;}};
  const identity=s=>s?`${s.district?.district_id||''}:${s.user?.id||s.district?.user_id||s.district?.email||''}`:'';
  let boundIdentity=identity(read());
  const error=(message,status=401)=>Object.assign(new Error(message),{status});
  function checked(){const s=read();if(s&&boundIdentity&&identity(s)!==boundIdentity)throw error('The signed-in account changed in another tab. Reload before continuing.');return s;}
  function notify(s){for(const listener of listeners)listener(s);}
  function save(incoming){
    const old=read(),same=identity(old)===identity(incoming),session={...(same?old:{}),...incoming};
    if(incoming.expires_in&&!incoming.expires_at)session.expires_at=Math.floor(Date.now()/1000)+incoming.expires_in;
    if(same&&!incoming.refresh_token&&old.access_token!==incoming.access_token&&expiry(old)>expiry(incoming)){session.access_token=old.access_token;session.expires_at=old.expires_at;session.expires_in=old.expires_in;}
    boundIdentity=identity(session);localStorage.setItem(key,JSON.stringify(session));notify(session);return session;
  }
  function clear(){boundIdentity='';localStorage.removeItem(key);notify(null);}
  function expiry(s){
    if(s?.expires_at)return Number(s.expires_at)*1000;
    try{const part=s.access_token.split('.')[1].replace(/-/g,'+').replace(/_/g,'/');return JSON.parse(atob(part)).exp*1000;}catch{return 0;}
  }
  function due(s){const end=expiry(s);return end>0&&end-Date.now()<120000;}
  async function renew(failedToken){
    const perform=async()=>{
      const session=checked();if(!session)return null;
      // Another request/tab may already have replaced the rejected token.
      if(failedToken&&session.access_token!==failedToken)return session;
      if(!failedToken&&!due(session))return session;
      if(!session.refresh_token){if(expiry(session)&&expiry(session)<=Date.now())throw error('Please sign in once more to enable automatic session renewal.');return session;}
      const response=await fetch('/api/auth/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({refresh_token:session.refresh_token}),cache:'no-store'});
      const data=await response.json().catch(()=>({}));
      if(!response.ok)throw error(response.status===401?'Please sign in again to renew your session.':(data.detail||'Session renewal is temporarily unavailable. Please retry.'),response.status);
      const latest=checked();
      if(!latest||identity(latest)!==identity(session))throw error('The session changed while renewing. Sign in again.');
      if(latest.access_token!==session.access_token||latest.refresh_token!==session.refresh_token)return latest;
      if(identity(data)!==identity(session))throw error('Session renewal returned a different account. Sign in again.');
      return save(data);
    };
    return navigator.locks?.request?navigator.locks.request('rendition-session-refresh',perform):perform();
  }
  async function ensure(failedToken){
    const session=checked();if(!session)return null;
    if(!failedToken&&!due(session))return session;
    if(!refreshing)refreshing=renew(failedToken).finally(()=>{refreshing=null;});
    return refreshing;
  }
  function prepare(options,session){
    const next={...options,headers:new Headers(options.headers||{})};
    if(session?.access_token){
      next.headers.set('Authorization','Bearer '+session.access_token);
      if(typeof next.body==='string'){try{const body=JSON.parse(next.body);if(body&&Object.hasOwn(body,'access_token'))next.body=JSON.stringify({...body,access_token:session.access_token});}catch{}}
    }
    return next;
  }
  async function request(url,options={}){
    const target=new URL(url,location.origin);
    if(target.origin!==location.origin)return fetch(url,options);
    if(['/api/bootstrap','/api/info'].includes(target.pathname)||/^\/api\/auth\/(login|signup|district-setup|refresh)$/.test(target.pathname))return fetch(url,options);
    const session=await ensure();
    if(!session&&(new Headers(options.headers||{}).has('Authorization')||/"access_token"\s*:\s*"[^"]+"/.test(options.body||'')))throw error('Please sign in to continue.');
    let response=await fetch(url,prepare(options,session));
    if(response.status===401&&session?.refresh_token){
      const renewed=await ensure(session.access_token);
      if(!renewed)throw error('Please sign in to continue.');
      response=await fetch(url,prepare(options,renewed));
    }
    return response;
  }
  function subscribe(listener){listeners.add(listener);return()=>listeners.delete(listener);}
  window.addEventListener('storage',event=>{if(event.key===key)notify(read());});
  const tick=()=>{if(document.visibilityState==='visible')ensure().catch(()=>{});};
  window.addEventListener('focus',tick);window.addEventListener('online',tick);document.addEventListener('visibilitychange',tick);
  setInterval(tick,60000);
  return {read,save,clear,ensure,fetch:request,subscribe,identity:()=>identity(checked())};
})();
