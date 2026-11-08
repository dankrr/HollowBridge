"""Durable observer of explicitly initiated Steam remote-install log entries."""
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

from .journal import Journal
from .service import local_registration
from .transport import request

_EVENT=re.compile(r'^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] CRemoteClientJobStartAppInstall::BYieldingRunClientJob starting appid ([1-9]\d{0,9}) on ([1-9]\d{0,19}) \([^()\r\n]{1,128}\)\s*$')


def parse(line, expected_client):
    if len(line)>512:return None
    match=_EVENT.fullmatch(line)
    if not match:return None
    appid=int(match.group(1)); client=match.group(2)
    return appid if appid<=0xffffffff and client==expected_client else None


class Outbox:
    def __init__(self,path,source_device):
        self.db=Journal(path)
        self.source_device=source_device
        with self.db._connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS native_cursor (key INTEGER PRIMARY KEY CHECK(key=1),dev INTEGER NOT NULL,ino INTEGER NOT NULL,offset INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS native_outbox (id TEXT PRIMARY KEY,appid INTEGER NOT NULL,source_hollow INTEGER NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);''')

    def scan(self,path,expected_client,registration=local_registration):
        path=Path(path)
        try: f=path.open('rb')
        except OSError:return
        with f, self.db._connect() as db:
            info=os.fstat(f.fileno());dev,ino=info.st_dev,info.st_ino
            db.execute('BEGIN IMMEDIATE')
            cursor=db.execute('SELECT dev,ino,offset FROM native_cursor WHERE key=1').fetchone()
            if cursor is None:
                # First run never replays historical actions.
                db.execute('INSERT INTO native_cursor VALUES (1,?,?,?)',(dev,ino,info.st_size));return
            if (cursor['dev'],cursor['ino'])!=(dev,ino):
                # Steam rotates to .previous.txt. Drain old inode before reading new.
                previous=path.with_name(path.stem+'.previous'+path.suffix)
                try:
                    with previous.open('rb') as old:
                        oldstat=os.fstat(old.fileno())
                        if (oldstat.st_dev,oldstat.st_ino)==(cursor['dev'],cursor['ino']):
                            old.seek(cursor['offset'])
                            oldoffset=self._consume(old,db,cursor['dev'],cursor['ino'],expected_client,registration)
                            if oldoffset>cursor['offset'] and oldoffset<oldstat.st_size:
                                db.execute('UPDATE native_cursor SET offset=? WHERE key=1',(oldoffset,));return
                except OSError:pass
                offset=0
            else: offset=cursor['offset'] if info.st_size>=cursor['offset'] else 0
            f.seek(offset);offset=self._consume(f,db,dev,ino,expected_client,registration)
            db.execute('UPDATE native_cursor SET dev=?,ino=?,offset=? WHERE key=1',(dev,ino,offset))

    def _consume(self,f,db,dev,ino,expected_client,registration):
        start=f.tell()
        data=f.read(65536)
        if not data:return start
        last=data.rfind(b'\n')
        if last<0:
            # Skip excessively long lines without emitting an event.
            return start+len(data) if len(data)==65536 else start
        consumed=0
        for raw in data[:last+1].splitlines(keepends=True):
            appid=parse(raw.decode('utf-8',errors='replace').rstrip('\r\n'),expected_client)
            if appid:
                material=f'{self.source_device}:{dev}:{ino}:{start+consumed}:'.encode()+raw
                rid='native_'+hashlib.sha256(material).hexdigest()
                db.execute('INSERT OR IGNORE INTO native_outbox(id,appid,source_hollow) VALUES (?,?,?)',(rid,appid,int(registration(appid))))
            consumed+=len(raw)
        return start+last+1

    def pending(self):
        with self.db._connect() as db:
            return [dict(r) for r in db.execute('SELECT id,appid,source_hollow FROM native_outbox WHERE delivered=0 ORDER BY rowid LIMIT 16')]

    def mark_delivered(self,rid):
        with self.db._connect() as db:db.execute('UPDATE native_outbox SET delivered=1 WHERE id=?',(rid,))


def deliver(discovery,port,key,row):
    peer=discovery.find()
    if not peer:raise RuntimeError('peer unavailable')
    payload={'id':row['id'],'appid':row['appid'],'source_hollow':bool(row['source_hollow'])}
    data=request(peer,port,key,'/native-events','POST',json.dumps(payload).encode())
    if data.get('id')!=row['id']:raise RuntimeError('mismatched native event response')


def run_observer(state,device,discovery,port,key,peer_client_id,stop=None,log=None):
    if not re.fullmatch(r'[1-9][0-9]{0,19}',peer_client_id):raise ValueError('invalid target Steam client ID')
    path=Path(log) if log else Path.home()/'.local/share/Steam/logs/remote_connections.txt'
    outbox=Outbox(Path(state)/'native-observer.sqlite3',device)
    next_delivery=0
    while stop is None or not stop.is_set():
        try:outbox.scan(path,peer_client_id)
        except (OSError,sqlite3.Error):pass
        now=time.monotonic()
        if now>=next_delivery:
            for row in outbox.pending():
                try:deliver(discovery,port,key,row);outbox.mark_delivered(row['id'])
                except (OSError,urllib.error.URLError,ValueError,KeyError,RuntimeError):break
            next_delivery=now+3
        if stop is not None:stop.wait(.5)
        else:time.sleep(.5)
