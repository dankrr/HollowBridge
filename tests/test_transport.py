import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from hollowbridge.service import Application, make_handler
from hollowbridge.transport import load_key, mac, request


class ExistingPeerKeyTest(unittest.TestCase):
    def test_reciprocal_remoteplaysync_configuration(self):
        import os
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'rps.conf'
            def write(device, peer, key):
                path.write_text(f'DEVICE_ID={device}\nPEER_ID={peer}\nPSK_HEX={key}\n')
                path.chmod(0o600)
            env = {'SLS_RPS_CONFIG':str(path)}
            with patch.dict(os.environ, env, clear=True):
                write('a'*32,'b'*32,'c'*64)
                first = load_key()
                write('b'*32,'a'*32,'c'*64)
                self.assertEqual(first, load_key())
                write('b'*32,'a'*32,'d'*64)
                self.assertNotEqual(first, load_key())
                write('b'*32,'e'*32,'c'*64)
                self.assertNotEqual(first, load_key())
                path.chmod(0o644)
                with self.assertRaises(ValueError): load_key()


class TransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.key = b'k' * 32
        app = Application(self.tmp.name, 'deck', None, 'decky', key=self.key, ready=lambda: True)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_paired_request_and_wrong_key(self):
        self.assertEqual(request('127.0.0.1', self.port, self.key, '/health')['device'], 'deck')
        with self.assertRaises((ValueError, OSError)):
            request('127.0.0.1', self.port, b'x' * 32, '/health')

    def test_captured_request_cannot_be_replayed(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('GET','/challenge')
        nonce = json.loads(conn.getresponse().read())['nonce']
        conn.close()
        headers = {'X-HollowBridge-Nonce': nonce, 'X-HollowBridge-Auth': mac(self.key, 'request', nonce, 'GET', '/health', b'')}
        for expected in (200, 403):
            conn = http.client.HTTPConnection('127.0.0.1', self.port)
            conn.request('GET', '/health', headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, expected)
            response.read(); conn.close()

class SyncIntegrationTest(unittest.TestCase):
    def test_authenticated_candidate_drives_one_preparation(self):
        import time
        from pathlib import Path
        from unittest.mock import patch
        from hollowbridge.sync_prepare import run_independent_observer
        from hollowbridge.journal import Journal

        class Ops:
            calls=[]
            def steam(self, action, appid): return {'visible':True}
            def store_signal(self, appid): return 'unknown'
            def prepare_synced(self, appid, kind):
                self.calls.append(appid)
                return {'result':'prepared'}
        class FastStop:
            event=threading.Event()
            def is_set(self): return self.event.is_set()
            def wait(self, seconds): return self.event.wait(min(seconds, .05))
        class Peer:
            def find(self): return '127.0.0.1'
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as dest:
            key=b'a'*32
            src=Application(source,'deck',None,'decky',key=key,ready=lambda:True)
            dst=Application(dest,'desktop',None,'desktop',key=key,ready=lambda:True,ops=Ops())
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(src))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            stop=FastStop(); candidates=[100]
            try:
                with patch('hollowbridge.sync_prepare.source_candidates',side_effect=lambda kind:list(candidates)), patch('hollowbridge.sync_prepare.source_hollow_evidence',return_value=False):
                    worker=threading.Thread(target=run_independent_observer,args=(dst,Peer(),server.server_address[1],stop),daemon=True);worker.start()
                    deadline=time.monotonic()+3
                    while time.monotonic()<deadline:
                        try:
                            with Journal(Path(dest)/'sync-preparation.sqlite3')._connect() as db:
                                if db.execute("SELECT count(*) FROM sync_apps WHERE state='baseline'").fetchone()[0]: break
                        except Exception: pass
                        time.sleep(.02)
                    else: self.fail('baseline was not recorded')
                    candidates.append(200)
                    while time.monotonic()<deadline and not dst.ops.calls: time.sleep(.02)
                    self.assertEqual(dst.ops.calls,[200])
                    time.sleep(.15)
                    self.assertEqual(dst.ops.calls,[200])
            finally:
                stop.event.set();worker.join(2);server.shutdown();server.server_close();thread.join()


class NativeEventTest(unittest.TestCase):
    def test_authenticated_native_event_delivery(self):
        from hollowbridge.native import deliver
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as state:
            key=b'b'*32
            app=Application(state,'deck',None,'decky',key=key,ready=lambda:True)
            row={'id':'native_'+'a'*64, 'appid':2005010, 'source_hollow':True}
            app.submit_native=Mock(return_value={'id':row['id']})
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(app))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                deliver(type('Peer',(),{'find':lambda self:'127.0.0.1'})(),server.server_address[1],key,row)
                app.submit_native.assert_called_once_with(row)
            finally:
                server.shutdown();server.server_close();thread.join()


if __name__ == '__main__': unittest.main()
