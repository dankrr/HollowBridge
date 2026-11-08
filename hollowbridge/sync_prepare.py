"""Experimental preparation-only observer and durable candidate queue.

Steam installation is never invoked here. The independent observer checks
source evidence and local visibility, not RemotePlaySync's internal snapshot.
"""
import time
import re
from pathlib import Path
from .journal import Journal


def source_candidates(kind, home=None):
    """Only apps RPS can publish with independent evidence of Hollow use."""
    home=Path.home() if home is None else Path(home)
    try:
        config=(home/'.config/SLSsteam/config.yaml').read_text()
        match=re.search(r'^AdditionalApps:[^\n]*\n(.*?)(?=^[A-Za-z_][A-Za-z_0-9]*:|\Z)',config,re.M|re.S)
        if not match:return None
        ids={int(x) for x in re.findall(r'^\s*-\s*([1-9]\d{0,9})(?:\s|#|$)',match.group(1),re.M)}
        return sorted(x for x in ids if source_hollow_evidence(x,kind,home))
    except (OSError,UnicodeError,ValueError):return None


def source_hollow_evidence(appid, kind, home=None):
    """Return a narrow local indication of Hollow use, never a license claim."""
    if type(appid) is not int or not 0 < appid <= 0xffffffff or kind not in ('desktop','decky'):
        return False
    home=Path.home() if home is None else Path(home)
    try:
        config=(home/'.config/SLSsteam/config.yaml').read_text()
        match=re.search(r'^AdditionalPackageApps:[^\n]*\n(.*?)(?=^[A-Za-z_][A-Za-z_0-9]*:|\Z)',config,re.M|re.S)
        if not match or not re.search(r'^\s*-\s*'+str(appid)+r'(?:\s|#|$)',match.group(1),re.M):return False
        cache=home/('.config/hollow-desktop/manifests' if kind=='desktop' else 'homebrew/data/hollow-decky/manifests')
        archive=cache/f'{appid}.zip'
        return archive.is_file() and archive.stat().st_size>0
    except (OSError,UnicodeError):return False


class PreparationQueue:
    def __init__(self, path):
        self.db = Journal(path)
        with self.db._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sync_origins (
                    origin TEXT PRIMARY KEY, session INTEGER NOT NULL, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sync_apps (
                    origin TEXT NOT NULL, appid INTEGER NOT NULL, present INTEGER NOT NULL,
                    state TEXT NOT NULL, updated INTEGER NOT NULL,
                    PRIMARY KEY (origin,appid));
            ''')
            # A process crash during preparation has an ambiguous outcome. Do not
            # repeat operations until a later explicit reconciliation is designed.
            db.execute("UPDATE sync_apps SET state='unknown' WHERE state='preparing'")

    def observe(self, origin, session, generation, appids):
        if (not isinstance(origin, str) or not origin or len(origin)>128 or
            type(session) is not int or type(generation) is not int or
            not 0<=session<2**53 or not 0<=generation<2**53 or
            not isinstance(appids, list) or len(appids)>20000 or
            any(type(x) is not int or not 0<x<=0xffffffff for x in appids) or
            appids != sorted(set(appids))):
            raise ValueError('invalid snapshot')
        now=int(time.time())
        with self.db._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT session,generation FROM sync_origins WHERE origin=?',(origin,)).fetchone()
            if previous and previous['session']==session and generation<=previous['generation']:
                return
            # Baseline avoids preparing the whole historical library on first run.
            baseline=previous is None
            known={r['appid']:r for r in db.execute('SELECT appid,present,state FROM sync_apps WHERE origin=?',(origin,))}
            for appid in appids:
                row=known.get(appid)
                if row is None:
                    db.execute('INSERT INTO sync_apps VALUES (?,?,?,?,?)',(origin,appid,1,'baseline' if baseline else 'pending',now))
                elif not row['present']:
                    # A source cache file may temporarily disappear. Neither
                    # that nor a re-add authorizes repeating prior work.
                    db.execute('UPDATE sync_apps SET present=1,updated=? WHERE origin=? AND appid=?',(now,origin,appid))
            current=set(appids)
            for appid,row in known.items():
                if row['present'] and appid not in current:
                    db.execute('UPDATE sync_apps SET present=0,updated=? WHERE origin=? AND appid=?',(now,origin,appid))
            db.execute('INSERT INTO sync_origins VALUES(?,?,?) ON CONFLICT(origin) DO UPDATE SET session=excluded.session,generation=excluded.generation',(origin,session,generation))

    def process_one(self, evidence, store_signal, prepare, kind, already=lambda _:False):
        """Prepare at most one new synchronized app; evidence is authenticated separately."""
        with self.db._connect() as db:
            rows=db.execute("SELECT origin,appid FROM sync_apps WHERE present=1 AND state='pending' ORDER BY updated,appid LIMIT 8").fetchall()
        target=None
        for row in rows:
            origin,appid=row['origin'],row['appid']
            try:
                if already(appid):
                    target='ignored'
                elif evidence(origin,appid):
                    target='ignored' if store_signal(appid)=='store_reported' else 'preparing'
                else:
                    # Peer may be offline or Hollow still preparing the source.
                    with self.db._connect() as db:
                        db.execute("UPDATE sync_apps SET updated=? WHERE origin=? AND appid=? AND state='pending'",(int(time.time())+1,origin,appid))
                    continue
            except Exception:
                with self.db._connect() as db:
                    db.execute("UPDATE sync_apps SET updated=? WHERE origin=? AND appid=? AND state='pending'",(int(time.time())+1,origin,appid))
                continue
            break
        if target is None:return False
        with self.db._connect() as db:
            cur=db.execute("UPDATE sync_apps SET state=?,updated=? WHERE origin=? AND appid=? AND present=1 AND state='pending'",(target,int(time.time()),origin,appid))
            if cur.rowcount!=1:return False
        if target=='ignored':return True
        try:
            result=prepare(appid,kind)
            state=('prepared_optional_missing' if result.get('optional_missing_depots') else 'prepared') if result.get('result')=='prepared' else 'unknown'
        except Exception:
            state='unknown'
        with self.db._connect() as db:
            db.execute("UPDATE sync_apps SET state=?,updated=? WHERE origin=? AND appid=? AND state='preparing'",(state,int(time.time()),origin,appid))
        return True


def run_independent_observer(app, discovery, port, stop):
    """Poll the authenticated source; wait for local Steam visibility before preparation.

    This observes independent source registrations and local visibility. It does
    not read or assert RemotePlaySync's internal acceptance state.
    """
    from .transport import request
    queue=PreparationQueue(app.state_dir/'sync-preparation.sqlite3')
    while not stop.is_set():
        try:
            if not app.ready():
                stop.wait(10);continue
            peer=discovery.find()
            if not peer:
                stop.wait(10);continue
            data=request(peer,port,app.key,'/sync-candidates')
            candidates=data['appids']
            queue.observe('configured-peer',0,int(time.time()*1000),candidates)
            current=set(candidates)
            def evidence(_origin,appid):
                if appid not in current:return False
                result=app.ops.steam('visible',appid)
                return isinstance(result,dict) and result.get('visible') is True
            queue.process_one(evidence,app.ops.store_signal,app.ops.prepare_synced,app.hollow_kind,
                              already=lambda appid:source_hollow_evidence(appid,app.hollow_kind))
        except Exception as error:
            print('HollowBridge preparation observer:',type(error).__name__,flush=True)
        stop.wait(10)
