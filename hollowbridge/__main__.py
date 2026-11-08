import argparse
import json
import fcntl
import os
import sys
import time
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from .service import Application, serve, process_identity_alive
from .transport import load_key, request

PORT=39482

def main():
    p=argparse.ArgumentParser(prog='hollowbridge');sp=p.add_subparsers(dest='command',required=True)
    s=sp.add_parser('serve');s.add_argument('--bind',default='0.0.0.0');s.add_argument('--peer',default=None,help='optional VPN peer address');s.add_argument('--key-file',required=True);s.add_argument('--device',required=True);s.add_argument('--hollow',choices=['desktop','decky'],required=True);s.add_argument('--port',type=int,default=PORT);s.add_argument('--state',default=str(Path.home()/'.local/state/hollowbridge'));s.add_argument('--socket-activated',action='store_true');s.add_argument('--idle-seconds',type=int,default=120);s.add_argument('--observe-native',action='store_true');s.add_argument('--observe-sync',action='store_true');s.add_argument('--peer-client-id');s.add_argument('--steam-pid',type=int);s.add_argument('--steam-start');s.add_argument('--native-log',help=argparse.SUPPRESS)
    u=sp.add_parser('units');u.add_argument('--bind',required=True);u.add_argument('--peer',required=True);u.add_argument('--device',required=True);u.add_argument('--hollow',choices=['desktop','decky'],required=True);u.add_argument('--port',type=int,default=PORT);u.add_argument('--state',default=str(Path.home()/'.local/state/hollowbridge'));u.add_argument('--idle-seconds',type=int,default=120);u.add_argument('--out',required=True);u.add_argument('--replace-managed',action='store_true')
    ui=sp.add_parser('ui');ui.add_argument('--peer',required=True);ui.add_argument('--port',type=int,default=PORT);ui.add_argument('--state',default=str(Path.home()/'.local/state/hollowbridge'))
    for name in ('health','status','watch','install'):
        q=sp.add_parser(name);q.add_argument('--peer',required=True);q.add_argument('--port',type=int,default=PORT);q.add_argument('--key-file',required=True)
        if name in ('status','watch'):q.add_argument('id')
        if name=='install':
            q.add_argument('appid',type=int);q.add_argument('--method',choices=['auto','native','hollow'],default='auto');q.add_argument('--hollow-fallback',action='store_true');q.add_argument('--id',default=None,help='reuse the same ID when retrying after a connection failure')
    args=p.parse_args()
    if args.command=='units':
        from .units import generate
        path=generate(args.out,args.bind,args.peer,args.device,args.hollow,args.port,args.state,args.idle_seconds,replace=args.replace_managed)
        print('Generated units in',path,'— not installed or enabled.')
        return 0
    if args.command=='serve':
        if bool(args.steam_pid) != bool(args.steam_start):raise SystemExit('provide both --steam-pid and --steam-start')
        if args.steam_pid and (args.steam_pid<1 or not args.steam_start.isdigit()):raise SystemExit('invalid Steam process identity')
        if args.steam_pid and not process_identity_alive(args.steam_pid,args.steam_start):return 0
        key=load_key(args.key_file)
        state=Path(args.state);state.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(state,0o700)
        lock=open(state/'agent.lock','a')
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('HollowBridge agent already running for this state directory')
        if args.socket_activated: raise SystemExit('socket activation is not supported by the LAN transport')
        if args.observe_native and (not args.peer_client_id or not args.peer_client_id.isdigit()):raise SystemExit('--observe-native requires --peer-client-id')
        observer={'peer':args.peer,'client_id':args.peer_client_id,'log':args.native_log} if args.observe_native else None
        steam_identity=(args.steam_pid,args.steam_start) if args.steam_pid else None
        app=Application(args.state,args.device,None,args.hollow,key=key)
        pidfile=state/'native-agent.pid'
        if steam_identity:
            stat_text=Path('/proc/self/stat').read_text()
            start=stat_text[stat_text.rfind(')')+2:].split()[19]
            record=f'{os.getpid()} {start}\n'
            fd,temp=tempfile.mkstemp(prefix='.native-agent-',dir=state)
            try:
                with os.fdopen(fd,'w') as f:f.write(record)
                os.replace(temp,pidfile)
            finally:
                if os.path.exists(temp):os.unlink(temp)
        try:
            return serve(args.bind,args.port,app,steam_identity,args.peer,observer)
        finally:
            if steam_identity:
                try:
                    if pidfile.read_text()==record:pidfile.unlink()
                except OSError:pass
    if args.command=='ui':
        from .ui import run
        return run(args.peer,args.port,args.state)
    key=load_key(args.key_file)
    path='/health' if args.command=='health' else '/requests/'+args.id if args.command in ('status','watch') else '/requests'
    body=b''
    method='GET'
    if args.command=='install':
        request_id=args.id or uuid.uuid4().hex
        print('request_id:',request_id, file=sys.stderr, flush=True)
        body=json.dumps({'id':request_id,'appid':args.appid,'method':args.method,'fallback':args.hollow_fallback}).encode()
        method='POST'
    while True:
        try:
            data=request(args.peer,args.port,key,path,method,body)
            print(json.dumps(data),flush=True)
            if args.command!='watch' or data.get('request',{}).get('state') in ('complete','failed','unknown','ignored'): break
        except (OSError,ValueError) as e:
            if args.command!='watch':
                print('Connection failed; retry an install using the same request ID:',e,file=sys.stderr)
                return 1
            print('Peer unavailable; reconnecting...',file=sys.stderr,flush=True)
        time.sleep(3)

    return 0

if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: print('HollowBridge stopped.',file=sys.stderr); sys.exit(0)
