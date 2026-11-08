// Account-side Steam Store signal. Credentials stay in this process and are sent only to Steam.
const mode=process.argv[2];
const appid=Number(mode);
if(mode!=='list'&&(!Number.isInteger(appid)||appid<1||appid>4294967295)) throw Error('invalid app id');
let ws;
try {
  const list=await(await fetch('http://127.0.0.1:8080/json/list',{signal:AbortSignal.timeout(3000)})).json();
  const t=list.find(x=>x.title==='SharedJSContext');
  if(!t?.webSocketDebuggerUrl?.startsWith('ws://127.0.0.1:8080/')) throw Error('unavailable');
  ws=new WebSocket(t.webSocketDebuggerUrl);
  const result=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(Error('timeout')),4000);
    ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Network.getCookies',params:{urls:['https://store.steampowered.com/']}}));
    ws.onerror=()=>reject(Error('unavailable'));
    ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id===1){clearTimeout(timer);resolve(m.result)}};
  });
  const cookies=(result?.cookies||[]).filter(c=>c.domain==='store.steampowered.com'||c.domain==='.steampowered.com');
  if(!cookies.length) throw Error('not signed in');
  const cookie=cookies.map(c=>c.name+'='+c.value).join('; ');
  const response=await fetch('https://store.steampowered.com/dynamicstore/userdata/',{headers:{cookie},redirect:'error',signal:AbortSignal.timeout(6000)});
  if(!response.ok||!(response.headers.get('content-type')||'').includes('json')) throw Error('unavailable');
  const data=await response.json();
  if(!Array.isArray(data.rgOwnedApps)||!data.rgOwnedApps.every(x=>Number.isSafeInteger(x))) throw Error('invalid response');
  console.log(JSON.stringify(mode==='list'?{apps:data.rgOwnedApps}:{signal:data.rgOwnedApps.includes(appid)?'store_reported':'unknown'}));
} catch {
  console.log(JSON.stringify(mode==='list'?{apps:[]}:{signal:'unknown'}));
} finally {ws?.close();}
