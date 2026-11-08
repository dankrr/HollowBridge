"""A small Tailscale-only receiver. No Steam debugging endpoint is exposed."""
import ipaddress
import json
import os
import re
import socket
import socketserver
import struct
import stat
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler

from . import adapter
from .journal import Journal, Conflict
from .manifest import parse_vdf, inspect_manifest
from .catalog import games


def tailscale_identity(ip):
    address = ipaddress.ip_address(ip)
    if address.version != 4 or address not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('not a Tailscale address')
    p = subprocess.run(['tailscale', 'whois', '--json', str(address)], capture_output=True, text=True, timeout=5, check=True)
    result = json.loads(p.stdout)
    return result['Node']['StableID']


def process_identity_alive(pid, start):
    try:
        stat_text=Path(f'/proc/{pid}/stat').read_text()
        return stat_text[stat_text.rfind(')')+2:].split()[19]==str(start)
    except (OSError,IndexError):
        return False


def steam_available():
    try:
        opener=build_opener(ProxyHandler({}))
        with opener.open('http://127.0.0.1:8080/json/list',timeout=2) as response:
            tabs=json.load(response)
        return any(t.get('title')=='SharedJSContext' for t in tabs)
    except Exception:
        return False


def local_registration(appid):
    path = Path.home()/'.config/SLSsteam/config.yaml'
    try: text = path.read_text()
    except OSError: return False
    # Only inspect the two app-id sections, never keys or other settings.
    for section in ('AdditionalApps', 'AdditionalPackageApps'):
        m = re.search(r'^'+section+r':[^\n]*\n(.*?)(?=^[A-Za-z_][A-Za-z_0-9]*:|\Z)', text, re.M|re.S)
        if m and re.search(r'^\s*-\s*'+str(appid)+r'(?:\s|#|$)', m.group(1), re.M): return True
    return False


def installed(appid):
    root = Path.home()/'.local/share/Steam/steamapps'
    paths = [root]
    try:
        folders = parse_vdf((root/'libraryfolders.vdf').read_text())['libraryfolders']
        for folder in folders.values():
            if isinstance(folder, dict) and isinstance(folder.get('path'), str):
                paths.append(Path(folder['path'])/'steamapps')
    except (OSError, KeyError, ValueError, TypeError): pass
    for folder in paths:
        result = inspect_manifest(folder/f'appmanifest_{appid}.acf', appid)
        if result['status'] == 'installed_with_content': return result
    return None


def reconcile_native_request(app, row, now=None):
    """Record content-based completion or an unresolved empty retry; never retry."""
    if installed(row['appid']):
        app.journal.transition(row['id'], row['state'], 'complete', 'Steam installed nonempty content.')
    elif row['state']=='observing' and (time.time() if now is None else now)-row['updated']>=30:
        from .native_recovery import find_manifest
        _,info=find_manifest(row['appid'])
        if info and (info.get('status')=='empty_install' or (info.get('state_flags',0)&0x20 and info.get('size_on_disk',0)==0)):
            app.journal.transition(row['id'],'observing','unknown','Steam still reports no installed content after the retry; no further automatic retry was made.')


class Application:
    def __init__(self, state, device, peer, hollow_kind, ops=adapter, authenticate=tailscale_identity, ready=steam_available):
        self.device, self.peer, self.hollow_kind = device, peer, hollow_kind
        self.state_dir=Path(state)
        self.ops, self.authenticate, self.ready = ops, authenticate, ready
        Path(state).mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(state, 0o700)
        self.journal=Journal(Path(state)/'requests.sqlite3')
        self.journal.recover()
        self.lock=threading.Lock()
        self.catalog_cache=(0,[])
        self.catalog_lock=threading.Lock()
        self.activity_lock=threading.Lock()
        self.active=0
        self.last_activity=time.monotonic()

    def begin(self):
        with self.activity_lock:
            self.active+=1;self.last_activity=time.monotonic()

    def end(self):
        with self.activity_lock:
            self.active-=1;self.last_activity=time.monotonic()

    def catalog(self):
        with self.catalog_lock:
            stamp,extra=self.catalog_cache
            if time.monotonic()-stamp>300:
                try: extra=self.ops.account_catalog(self.hollow_kind)
                except Exception: extra=[]
                self.catalog_cache=(time.monotonic(),extra)
            found={g['appid']:g for g in games()}
            for entry in extra:
                if not isinstance(entry,dict) or type(entry.get('appid')) is not int:continue
                appid=entry['appid'];name=str(entry.get('name') or appid)[:200]
                item=found.setdefault(appid,{'appid':appid,'name':name,'hollow_registered':False,'installed':False})
                if item['name']==str(appid):item['name']=name
                item['store_reported']=True
            return sorted(found.values(),key=lambda x:(x['name'].casefold(),x['appid']))

    def submit(self, data):
        if set(data) != {'id','appid','method','fallback'} or data['method'] not in ('auto','native','hollow'): raise ValueError('invalid request fields')
        with self.lock:
            row = self.journal.accept(data['id'], data['appid'], self.device, data['method'], data['fallback'])
            # Only the winning transition can execute a side effect. Replays only return status.
            if self.journal.transition(row['id'], 'accepted', 'dispatching'):
                self.begin()
                def worker():
                    try:self.run(row)
                    finally:self.end()
                threading.Thread(target=worker, daemon=True).start()
            return self.journal.get(row['id'])

    def submit_native(self,data):
        if set(data)!={'id','appid','source_hollow'} or type(data['source_hollow']) is not bool:
            raise ValueError('invalid native event')
        if not isinstance(data['id'],str) or not re.fullmatch(r'native_[a-f0-9]{64}',data['id']):
            raise ValueError('invalid native event id')
        with self.lock:
            row=self.journal.accept(data['id'],data['appid'],self.device,'native_event',data['source_hollow'])
            if self.journal.transition(row['id'],'accepted','dispatching'):
                self.begin()
                def worker():
                    try:self.run_native(row)
                    finally:self.end()
                threading.Thread(target=worker,daemon=True).start()
            return self.journal.get(row['id'])

    def run_native(self,row):
        from .native_recovery import recover
        try:
            state,detail=recover(row['appid'],bool(row['fallback']),self.ops,local_registration,self.ready,self.hollow_kind)
            if state not in ('observing','ignored','failed','unknown'):state='unknown'
            self.journal.transition(row['id'],'dispatching',state,detail)
        except Exception:
            self.journal.transition(row['id'],'dispatching','unknown','Native recovery failed or timed out; inspect Steam before retrying.')

    def run(self, row):
        request_id, appid = row['id'], row['appid']
        try:
            if not self.ready():
                self.journal.transition(request_id,'dispatching','failed','Steam is not available on the destination.');return
            if installed(appid):
                self.journal.transition(request_id,'dispatching','observing')
                self.journal.transition(request_id,'observing','complete'); return
            method=row['method']
            signal = self.ops.store_signal(appid) if method == 'auto' and hasattr(self.ops,'store_signal') else 'unknown'
            use_hollow=method == 'hollow' or (method == 'auto' and signal != 'store_reported' and local_registration(appid))
            if use_hollow:
                prepared=self.ops.prepare(appid,self.hollow_kind)
                if prepared.get('result') != 'prepared':
                    self.journal.transition(request_id,'dispatching', 'failed' if prepared.get('result')=='failed' else 'unknown', 'Hollow preparation did not complete; inspect destination Hollow logs.'); return
            result=self.ops.steam('steam_install',appid)
            if result.get('result')=='no_content' and not use_hollow and row['fallback']:
                prepared=self.ops.prepare(appid,self.hollow_kind)
                if prepared.get('result') != 'prepared':
                    self.journal.transition(request_id,'dispatching','unknown','Hollow fallback did not complete; inspect destination Hollow logs.'); return
                result=self.ops.steam('steam_install',appid)
            value=result.get('result')
            if value=='started': self.journal.transition(request_id,'dispatching','observing','Steam accepted the request; waiting for installed content.')
            elif value in ('busy','no_content','failed'): self.journal.transition(request_id,'dispatching','failed', {'busy':'Steam has another install wizard open.','no_content':'Steam selected zero content; check license and preparation.','failed':'Steam rejected the installation.'}[value])
            else: self.journal.transition(request_id,'dispatching','unknown','Steam needs attention or the result is uncertain; inspect the destination before retrying.')
        except Exception:
            # A timeout or process crash after dispatch is ambiguous. Never replay it automatically.
            self.journal.transition(request_id,'dispatching','unknown','Local adapter failed or timed out; inspect Steam and Hollow before retrying.')

    def status(self, request_id):
        row=self.journal.get(request_id)
        if not row: return None
        result={'request':row,'steam':None}
        if row['state'] in ('observing','unknown','complete'):
            content=installed(row['appid'])
            if content:
                result['content']=content
                if row['state']!='complete':
                    self.journal.transition(request_id,row['state'],'complete')
                    result['request']=self.journal.get(request_id)
            elif row['state']!='complete':
                try:
                    result['steam']=self.ops.steam('status',row['appid'])
                    if isinstance(result['steam'],dict) and result['steam'].get('completed'):
                        result['warning']='Steam reports completion, but installed content is not yet verified.'
                except Exception: result['steam']={'unavailable':True}
            else:
                result['warning']='The previously verified installation is no longer present.'
        return result


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.0'
        def handle(self):
            app.begin()
            try: super().handle()
            finally: app.end()
        def log_message(self, *_): pass
        def response(self, code, obj):
            raw=json.dumps(obj,separators=(',',':')).encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def authorized(self):
            if self.connection.family == socket.AF_UNIX:
                try: return struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]==os.getuid()
                except OSError: return False
            try: return app.authenticate(self.client_address[0]) == app.peer
            except Exception: return False
        def do_GET(self):
            if not self.authorized(): return self.response(403,{'error':'unauthorized'})
            path=urlsplit(self.path).path
            if path=='/health': return self.response(200,{'device':app.device,'available':True,'steam_available':app.ready(),'hollow_adapter':app.hollow_kind})
            if path=='/sync-candidates':
                from .sync_prepare import source_candidates
                ids=source_candidates(app.hollow_kind)
                return self.response(200,{'appids':ids}) if ids is not None else self.response(503,{'error':'source registrations unavailable'})
            if path=='/games': return self.response(200,{'device':app.device,'games':app.catalog()})
            if path=='/requests': return self.response(200,{'requests':app.journal.recent()})
            match=re.fullmatch(r'/requests/([A-Za-z0-9_-]{16,128})',path)
            if not match: return self.response(404,{'error':'not found'})
            status=app.status(match.group(1))
            return self.response(200,status) if status else self.response(404,{'error':'not found'})
        def do_POST(self):
            if not self.authorized(): return self.response(403,{'error':'unauthorized'})
            if self.path not in ('/requests','/native-events'): return self.response(404,{'error':'not found'})
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 1 <= n <= 4096: raise ValueError('invalid body size')
                body=json.loads(self.rfile.read(n))
                if not isinstance(body,dict): raise ValueError('invalid body')
                result=app.submit_native(body) if self.path=='/native-events' else app.submit(body)
                return self.response(202,result)
            except Conflict as e: return self.response(409,{'error':str(e)})
            except (ValueError,TypeError,json.JSONDecodeError): return self.response(400,{'error':'invalid request'})
    return Handler


class LocalServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads=True


def _activated_sockets(bind,port,app):
    if os.environ.get('LISTEN_PID')!=str(os.getpid()) or os.environ.get('LISTEN_FDS')!='2':
        raise ValueError('expected two systemd-activated listening sockets')
    for name in ('LISTEN_PID','LISTEN_FDS','LISTEN_FDNAMES'):os.environ.pop(name,None)
    inet=unix=None
    for fd in (3,4):
        sock=socket.socket(fileno=fd)
        if sock.getsockopt(socket.SOL_SOCKET,socket.SO_ACCEPTCONN)!=1:raise ValueError('activation fd is not listening')
        if sock.family==socket.AF_INET:
            device=sock.getsockopt(socket.SOL_SOCKET,socket.SO_BINDTODEVICE,32).rstrip(b'\0')
            if sock.getsockname()!=(bind,port) or device!=b'tailscale0' or inet:raise ValueError('unsafe activated network socket')
            inet=sock
        elif sock.family==socket.AF_UNIX:
            path=app.state_dir/'agent.sock'
            if sock.getsockname()!=str(path) or unix:raise ValueError('unsafe activated local socket')
            info=path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:raise ValueError('unsafe local socket permissions')
            unix=sock
        else: raise ValueError('unexpected activation socket')
    if not inet or not unix:raise ValueError('missing activation socket')
    return inet,unix


def serve(bind,port,app,activated=False,idle_seconds=120,observer=None,steam_identity=None,sync_peer=None):
    address=ipaddress.ip_address(bind)
    if address.version!=4 or address not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('agent must bind to its Tailscale IPv4 address')
    own = subprocess.run(['tailscale','ip','-4'], capture_output=True, text=True, check=True, timeout=5).stdout.split()
    if bind not in own: raise ValueError('bind address is not this device’s Tailscale IP')
    class TailnetServer(ThreadingHTTPServer):
        def server_bind(self):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'tailscale0\0')
            super().server_bind()
    socket_path=app.state_dir/'agent.sock'
    if activated:
        inet,unix=_activated_sockets(bind,port,app)
        server=TailnetServer((bind,port),make_handler(app),bind_and_activate=False)
        server.socket.close();server.socket=inet;server.server_address=inet.getsockname()
        server.server_name=bind;server.server_port=port
        local=LocalServer(str(socket_path),make_handler(app),bind_and_activate=False)
        local.socket.close();local.socket=unix;local.server_address=str(socket_path)
    else:
        server=TailnetServer((bind,port),make_handler(app))
        if socket_path.exists() or socket_path.is_symlink():
            info=socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid(): raise ValueError('unsafe local socket path')
            socket_path.unlink()
        local=LocalServer(str(socket_path),make_handler(app));os.chmod(socket_path,0o600)
    threading.Thread(target=local.serve_forever,daemon=True).start()
    stop_event=threading.Event()
    if sync_peer:
        from .sync_prepare import run_independent_observer
        def supervise_sync():
            while not stop_event.is_set():
                try:run_independent_observer(app,sync_peer,port,stop_event)
                except Exception as error:
                    print('HollowBridge preparation observer stopped:',type(error).__name__,flush=True)
                    if stop_event.wait(5):return
        threading.Thread(target=supervise_sync,daemon=True).start()
    if observer:
        from .native import run_observer
        def supervise_observer():
            while not stop_event.is_set():
                try:run_observer(str(app.state_dir),app.device,observer['peer'],port,observer['client_id'],stop_event,observer.get('log'))
                except Exception as error:
                    print('HollowBridge observer stopped:',type(error).__name__,flush=True)
                    if stop_event.wait(5):return
        threading.Thread(target=supervise_observer,daemon=True).start()
    def reconcile_native():
        while not stop_event.wait(5):
            for row in app.journal.unsettled_native():
                try:
                    reconcile_native_request(app,row)
                except Exception:pass
    threading.Thread(target=reconcile_native,daemon=True).start()
    if steam_identity:
        def check_steam():
            pid,start=steam_identity
            while not stop_event.wait(3):
                if not process_identity_alive(pid,start):break
            if not stop_event.is_set():server.shutdown()
        threading.Thread(target=check_steam,daemon=True).start()
    if activated and not observer and not sync_peer:
        def idle_monitor():
            while True:
                time.sleep(2)
                with app.activity_lock:
                    idle=app.active==0 and time.monotonic()-app.last_activity>=idle_seconds
                if idle:server.shutdown();return
        threading.Thread(target=idle_monitor,daemon=True).start()
    try: server.serve_forever()
    finally:
        stop_event.set()
        local.shutdown();local.server_close();server.server_close()
        if not activated:socket_path.unlink(missing_ok=True)
