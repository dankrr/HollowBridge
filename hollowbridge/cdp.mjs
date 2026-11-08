// Destination-local Steam/Decky adapter. Nothing here listens on the network.
const [action, arg] = process.argv.slice(2);
const appid = Number(arg);
if (!Number.isInteger(appid) || appid < 1 || appid > 4294967295) throw Error('invalid app id');
const tabs = await (await fetch('http://127.0.0.1:8080/json/list', {signal: AbortSignal.timeout(3000)})).json();
const tab = tabs.find(t => t.title === 'SharedJSContext');
if (!tab || !tab.webSocketDebuggerUrl.startsWith('ws://127.0.0.1:8080/')) throw Error('Steam local debugging interface unavailable');
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen=resolve; ws.onerror=reject; setTimeout(()=>reject(Error('Steam connection timed out')),3000).unref(); });
let id=0;
async function evalJS(expression, timeout=30000) {
  const n=++id;
  return await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>{ws.removeEventListener('message', handler);reject(Error('Steam operation timed out'));},timeout);
    function handler(event){const m=JSON.parse(event.data); if(m.id!==n)return; clearTimeout(timer);ws.removeEventListener('message',handler); if(m.error || m.result?.exceptionDetails) return reject(Error('Steam operation failed')); resolve(m.result.result.value);}
    ws.addEventListener('message',handler); ws.send(JSON.stringify({id:n,method:'Runtime.evaluate',params:{expression,returnByValue:true,awaitPromise:true}}));
  });
}
try {
  let result;
  if (action === 'visible') {
    result=JSON.parse(await evalJS(`(async()=>JSON.stringify({visible:await SteamClient.Apps.GetIsSubscribedApp(${appid})}))()`));
  } else if (action === 'compat') {
    result=JSON.parse(await evalJS(`new Promise(resolve=>{let h;h=SteamClient.Apps.RegisterForAppDetails(${appid},x=>{resolve(JSON.stringify({tool:x?.strCompatToolName||'',priority:x?.nCompatToolPriority??null}));setTimeout(()=>h?.unregister?.(),0)});setTimeout(()=>resolve('null'),4000)})`));
  } else if (action === 'compat_proton') {
    result=JSON.parse(await evalJS(`(async()=>{await SteamClient.Apps.SpecifyCompatTool(${appid},'proton_experimental');return JSON.stringify({result:'requested'})})()`));
  } else if (action === 'decky_details') {
    result=JSON.parse(await evalJS(`(async()=>{let loader=window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;if(!loader)return 'null';let api=loader.connect(2,'hollow-decky');let d=await api.callable('get_details')('${appid}');return JSON.stringify({depots:d?.depots||null,error:d?.error||null})})()`,30000));
  } else if (action === 'status') {
    result=await evalJS(`new Promise(resolve=>{let h;h=SteamClient.Downloads.RegisterForDownloadItems((all,clients)=>{let item=clients.flatMap(c=>c.remote_client_id==='0'?c.item_data:[]).find(x=>x.appid===${appid});resolve(item?JSON.stringify({active:item.active,paused:item.paused,completed:item.completed,error:item.update_error,result:item.update_result,percent:item.overall_percent_complete,progress:item.update_type_info?.[0]?.progress?.map(p=>({bytes:p.bytes_in_progress,total:p.bytes_total}))}):'null');setTimeout(()=>h?.unregister?.(),0)});setTimeout(()=>resolve('null'),2500)})`);
    result=JSON.parse(result);
  } else if(action === 'steam_install') {
    result=await evalJS(`(async()=>{let m=await SteamClient.Installs.GetInstallManagerInfo();if(![0,14,15,16].includes(m.eInstallState))return JSON.stringify({result:'busy'});SteamClient.Installs.OpenInstallWizard([${appid}],"0");for(let i=0;i<80;i++){await new Promise(r=>setTimeout(r,250));m=await SteamClient.Installs.GetInstallManagerInfo();if(m.currentAppID!==${appid})continue;if(m.eInstallState===7){if(m.nDiskSpaceRequired<=0){SteamClient.Installs.CancelInstall();return JSON.stringify({result:'no_content'});}SteamClient.Installs.ContinueInstall();return JSON.stringify({result:'started'});}if([8,4,6,13].includes(m.eInstallState))return JSON.stringify({result:'needs_user_action'});if(m.eInstallState===15)return JSON.stringify({result:'failed',error:m.errorDetail});if([9,10,14].includes(m.eInstallState))return JSON.stringify({result:'started'});}return JSON.stringify({result:'unknown'});})()`,25000);
    result=JSON.parse(result);
  } else if (action === 'decky_prepare') {
    result=await evalJS(`(async()=>{let loader=window.__DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit;if(!loader)return JSON.stringify({result:'unavailable'});let api=loader.connect(2,'hollow-decky');let r=await api.callable('inject_game')('${appid}');if(!r?.injected)return JSON.stringify({result:'failed',error:r?.error||'Hollow rejected request'});for(let i=0;i<150;i++){await new Promise(r=>setTimeout(r,1000));let s=await api.callable('get_inject_status')('${appid}');if(s?.done){let error=s.error||s.manifest_error||s.depot_keys_error||(s.keys_missing?.length?'Hollow reported missing depot keys':null);return JSON.stringify({result:error?'failed':'prepared',error,missing_depots:s.keys_missing||[]});}}return JSON.stringify({result:'unknown'});})()`,180000);
    result=JSON.parse(result);
  } else throw Error('unknown action');
  console.log(JSON.stringify(result));
} finally {ws.close();}
