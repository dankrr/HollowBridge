"""Durable, fail-closed journal for future install adapters."""
import re
from contextlib import contextmanager
import sqlite3
import time
from pathlib import Path


class Conflict(ValueError):
    pass


class Journal:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, appid INTEGER NOT NULL, destination TEXT NOT NULL,
                    state TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
                    method TEXT NOT NULL DEFAULT 'auto', fallback INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_app_destination
                ON requests(appid, destination)
                WHERE state IN ('accepted', 'dispatching', 'observing', 'unknown');
            ''')
            cols = {r['name'] for r in db.execute('PRAGMA table_info(requests)')}
            if 'detail' not in cols: db.execute("ALTER TABLE requests ADD COLUMN detail TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def accept(self, request_id: str, appid: int, destination: str, method='auto', fallback=False) -> dict:
        if (not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', request_id)
                or type(appid) is not int or not 0 < appid <= 4294967295
                or method not in ('auto', 'native', 'hollow', 'native_event') or type(fallback) is not bool
                or not isinstance(destination, str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,128}', destination)):
            raise ValueError('invalid request')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            if row:
                if (row['appid'], row['destination'], row['method'], row['fallback']) != (appid, destination, method, int(fallback)):
                    raise Conflict('request id already belongs to another installation')
                return dict(row)
            now = int(time.time())
            try:
                db.execute('INSERT INTO requests (id,appid,destination,state,created,updated,method,fallback) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (request_id, appid, destination, 'accepted', now, now, method, int(fallback)))
            except sqlite3.IntegrityError as e:
                raise Conflict('an installation is already active or has an unknown outcome') from e
            return dict(db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone())

    def transition(self, request_id: str, expected: str, new: str, detail: str = '') -> bool:
        permitted = {('accepted', 'dispatching'), ('accepted', 'failed'),
                     ('dispatching', 'observing'), ('dispatching', 'unknown'), ('dispatching', 'failed'), ('dispatching', 'ignored'),
                     ('observing', 'complete'), ('observing', 'failed'),
                     ('observing', 'unknown'), ('unknown', 'complete'), ('unknown', 'failed')}
        if (expected, new) not in permitted:
            raise ValueError('invalid transition')
        with self._connect() as db:
            cur = db.execute('UPDATE requests SET state=?, updated=?, detail=? WHERE id=? AND state=?',
                             (new, int(time.time()), str(detail)[:300], request_id, expected))
            return cur.rowcount == 1

    def get(self, request_id):
        with self._connect() as db:
            row = db.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
            return dict(row) if row else None

    def recent(self, limit=100):
        with self._connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM requests ORDER BY updated DESC LIMIT ?', (max(1,min(int(limit),100)),))]

    def unsettled_native(self):
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM requests WHERE method='native_event' AND state IN ('observing','unknown') ORDER BY updated LIMIT 100")]

    def recover(self):
        """Never replay after a crash: the external operation may have occurred."""
        with self._connect() as db:
            db.execute("UPDATE requests SET state='unknown', detail='Agent restarted during dispatch; inspect Steam before retrying.', updated=? WHERE state='dispatching'", (int(time.time()),))
