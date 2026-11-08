"""Local adapters; existing Hollow and Steam retain all installation logic."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CDP = Path(__file__).with_name('cdp.mjs')


def _run(args, timeout):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    if p.returncode:
        raise RuntimeError('Local adapter failed; check the local application logs')
    try: return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError): raise RuntimeError('Local adapter returned an invalid response') from None


def steam(action, appid):
    return _run(['node', str(CDP), action, str(appid)], 190 if action == 'decky_prepare' else 35)


def prepare(appid, kind):
    if kind == 'decky':
        return steam('decky_prepare', appid)
    if kind != 'desktop': raise RuntimeError('Unknown Hollow adapter')
    # Invoke the installed backend in its own process with its normal local environment.
    backend = Path.home() / '.local/share/hollow-desktop/current/backend'
    if not (backend / 'hollow/inject.py').is_file(): raise RuntimeError('Hollow Desktop backend not found')
    script = '''import json,time\nfrom hollow.inject import inject_game_sync,get_inject_status\nr=inject_game_sync(%r)\nif not r.get("injected"):\n print(json.dumps({"result":"failed","error":r.get("error","Hollow rejected request")}));raise SystemExit\nfor i in range(150):\n s=get_inject_status(%r) or {}\n if s.get("done"):\n  error=s.get("error") or s.get("manifest_error") or s.get("depot_keys_error")\n  if s.get("keys_missing") and not error: error="Hollow reported missing depot keys"\n  print(json.dumps({"result":"failed" if error else "prepared","error":error,"missing_depots":s.get("keys_missing") or []}));break\n time.sleep(1)\nelse: print(json.dumps({"result":"unknown"}))\n''' % (str(appid), str(appid))
    env = os.environ.copy(); env['PYTHONPATH'] = str(backend)
    p = subprocess.run([sys.executable, '-u', '-c', script], env=env, capture_output=True, text=True, timeout=190)
    if p.returncode: raise RuntimeError('Hollow Desktop preparation failed; check local logs')
    try: return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError): raise RuntimeError('Hollow Desktop returned an invalid response') from None


def store_signal(appid):
    # Never expose Steam cookies or the complete account list to another device.
    try:
        result = _run(['node', str(Path(__file__).with_name('store.mjs')), str(appid)], 12)
        return result.get('signal') if result.get('signal') in ('store_reported','unknown') else 'unknown'
    except (RuntimeError, subprocess.TimeoutExpired, OSError):
        return 'unknown'


def base_preparation_ready(appid,kind):
    """Ask the installed Hollow libraries to check base keys and manifests."""
    backend=(Path.home()/'.local/share/hollow-desktop/current/backend' if kind=='desktop'
             else Path.home()/'homebrew/plugins/hollow-decky')
    script='''import json,sys
from steam.appdetails import get_app_details
from steam.dlc_labels import real_unkeyed_depots
from sls.config.readers import get_configured_depot_keys
from hollow.manifests import depots_missing_manifests
from steam.paths import depotcache_dir
x=get_app_details(sys.argv[1]); ids=list(x.get('depots',{})); real=x.get('depot_has_manifests',{})
keys=get_configured_depot_keys(); configured=keys.get('keys',{}); cache=depotcache_dir()
ready=bool(not x.get('error') and ids and any(real.get(d) for d in ids) and cache and cache.is_dir() and isinstance(configured,dict) and not real_unkeyed_depots(x,set(configured),ids) and not depots_missing_manifests(ids,x))
print(json.dumps({'ready':ready}))
'''
    env=os.environ.copy();env['PYTHONPATH']=str(backend)
    p=subprocess.run([sys.executable,'-c',script,str(appid)],env=env,capture_output=True,text=True,timeout=60)
    if p.returncode:return False
    try:return json.loads(p.stdout.strip().splitlines()[-1]).get('ready') is True
    except (ValueError,IndexError,AttributeError):return False


def prepare_synced(appid, kind):
    """Prepare an absent game; never call Steam's installer."""
    from .native_recovery import find_manifest
    path,info=find_manifest(appid)
    if path is not None or info is not None:
        return {'result':'unknown','reason':'A Steam installation or ambiguous manifest already exists.'}
    if kind=='decky':
        details=steam('decky_details',appid)
    elif kind=='desktop':
        backend=Path.home()/'.local/share/hollow-desktop/current/backend'
        script="import json,sys\nfrom steam.appdetails import get_app_details\nd=get_app_details(sys.argv[1]);print(json.dumps({'depots':d.get('depots'),'error':d.get('error')}))"
        env=os.environ.copy();env['PYTHONPATH']=str(backend)
        p=subprocess.run([sys.executable,'-c',script,str(appid)],env=env,capture_output=True,text=True,timeout=50)
        if p.returncode:raise RuntimeError('Hollow metadata lookup failed')
        details=json.loads(p.stdout.strip().splitlines()[-1])
    else:raise RuntimeError('Unknown Hollow adapter')
    depots=details.get('depots') if isinstance(details,dict) else None
    if not isinstance(depots,dict) or not depots or details.get('error'):
        return {'result':'unknown','reason':'Hollow depot metadata is unavailable.'}
    platforms=[str(d.get('platform') or '').lower() for d in depots.values() if isinstance(d,dict)]
    if len(platforms)!=len(depots):return {'result':'unknown','reason':'Incomplete depot platform information.'}
    prepared=prepare(appid,kind)
    optional_missing=[]
    if prepared.get('result')!='prepared':
        missing=prepared.get('missing_depots')
        base_ids={str(x) for x in depots}
        # Hollow checks base and DLC keys together. Missing optional DLC is
        # reported separately; it must not be confused with a missing base key.
        if not (prepared.get('error')=='Hollow reported missing depot keys' and
                isinstance(missing,list) and missing and
                all(isinstance(x,(str,int)) for x in missing) and
                not (base_ids & {str(x) for x in missing})):
            return prepared
        optional_missing=missing
    if not base_preparation_ready(appid,kind):
        return {'result':'unknown','reason':'Hollow could not verify the base depot keys and manifests.'}
    # Do not change compatibility for a game Steam began installing meanwhile.
    path,info=find_manifest(appid)
    if path is not None or info is not None:return {'result':'unknown','reason':'Steam created a manifest during preparation.'}
    if all(x=='windows' for x in platforms):
        compat=steam('compat',appid)
        if not isinstance(compat,dict) or type(compat.get('priority')) is not int:
            return {'result':'unknown','reason':'Steam compatibility selection is unavailable.'}
        tool=compat.get('tool','').lower()
        native_tool=not tool or tool=='native' or tool.startswith('steamlinuxruntime')
        if native_tool and compat['priority']>=250:
            return {'result':'unknown','reason':'An explicit native compatibility choice needs attention.'}
        if native_tool:
            result=steam('compat_proton',appid)
            if result.get('result')!='requested':return {'result':'unknown','reason':'Steam compatibility change was not accepted.'}
            confirmed=steam('compat',appid)
            if not isinstance(confirmed,dict) or confirmed.get('tool')!='proton_experimental':
                return {'result':'unknown','reason':'Steam compatibility change could not be confirmed.'}
    return {'result':'prepared','optional_missing_depots':optional_missing}


def account_catalog(kind):
    """Account-side IDs and names from the installed Hollow Steam metadata reader."""
    store = _run(['node',str(Path(__file__).with_name('store.mjs')),'list'],12)
    ids=store.get('apps',[])
    if not isinstance(ids,list):return []
    ids=[x for x in ids if type(x) is int and 0<x<=4294967295][:20000]
    backend=(Path.home()/'.local/share/hollow-desktop/current/backend' if kind=='desktop' else Path.home()/'homebrew/plugins/hollow-decky')
    script="import json,sys\nfrom steam.appinfo import load_appinfo\nids=set(json.loads(sys.stdin.read())); db=load_appinfo()\nprint(json.dumps([{'appid':i,'name':(db.get(i) or {}).get('name') or str(i)} for i in ids]))\n"
    env=os.environ.copy();env['PYTHONPATH']=str(backend)
    try:
        p=subprocess.run([sys.executable,'-c',script],input=json.dumps(ids),capture_output=True,text=True,timeout=20,env=env)
        if p.returncode:return [{'appid':i,'name':str(i)} for i in ids]
        result=json.loads(p.stdout)
        return result if isinstance(result,list) else []
    except (OSError,ValueError,subprocess.TimeoutExpired):return [{'appid':i,'name':str(i)} for i in ids]
