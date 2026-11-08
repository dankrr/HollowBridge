"""Paired LAN discovery and authenticated requests, independent of RemotePlaySync."""
import hashlib
import hmac
import http.client
import json
import secrets
import socket
import threading
import time

MAGIC = b'HollowBridge-1 '
MAX_RESPONSE = 1024 * 1024


def mac(key, *parts):
    return hmac.new(key, b'\0'.join(p if isinstance(p, bytes) else str(p).encode() for p in parts), hashlib.sha256).hexdigest()


def load_key(path):
    with open(path, 'r', encoding='ascii') as f:
        value = f.read().strip()
    if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid pairing key')
    return bytes.fromhex(value)


class Challenges:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}

    def issue(self):
        now = time.monotonic()
        nonce = secrets.token_hex(32)
        with self.lock:
            self.pending = {n: expiry for n, expiry in self.pending.items() if expiry > now}
            if len(self.pending) >= 1024:
                return None
            self.pending[nonce] = now + 30
        return nonce

    def consume(self, nonce):
        with self.lock:
            expiry = self.pending.pop(nonce, 0)
        return expiry > time.monotonic()


class Discovery:
    def __init__(self, key, device, port, peer=None):
        self.key, self.device, self.port, self.peer = key, device, port, peer
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(('', port))
        self.sock.settimeout(1)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self.stopped.is_set():
            try:
                data, addr = self.sock.recvfrom(256)
                parts = data.split(b' ')
                if len(parts) != 4 or parts[0] != b'HollowBridge-1' or parts[1] != b'find':
                    continue
                role, nonce = parts[2], parts[3]
                if role == self.device.encode() or role not in (b'desktop', b'deck') or len(nonce) != 64:
                    continue
                digest = mac(self.key, b'discovery', role, self.device, nonce)
                self.sock.sendto(MAGIC + b'here ' + self.device.encode() + b' ' + nonce + b' ' + digest.encode(), addr)
            except socket.timeout:
                continue
            except OSError:
                break

    def find(self):
        if self.peer:
            return self.peer
        nonce = secrets.token_hex(32).encode()
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe.settimeout(2)
            probe.sendto(MAGIC + b'find ' + self.device.encode() + b' ' + nonce, ('255.255.255.255', self.port))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    data, addr = probe.recvfrom(256)
                except socket.timeout:
                    break
                parts = data.split(b' ')
                if len(parts) != 5 or parts[0:2] != [b'HollowBridge-1', b'here']:
                    continue
                role, got_nonce, signature = parts[2:]
                if role == self.device.encode() or role not in (b'desktop', b'deck') or got_nonce != nonce:
                    continue
                expected = mac(self.key, b'discovery', self.device, role, nonce).encode()
                if hmac.compare_digest(signature, expected):
                    return addr[0]
            return None
        finally:
            probe.close()

    def close(self):
        self.stopped.set()
        self.sock.close()
        self.thread.join(timeout=2)


def request(host, port, key, path, method='GET', body=b''):
    conn = http.client.HTTPConnection(host, port, timeout=6)
    try:
        conn.request('GET', '/challenge')
        response = conn.getresponse()
        raw = response.read(512)
        if response.status != 200:
            raise OSError('peer challenge unavailable')
        nonce = json.loads(raw)['nonce']
        if not isinstance(nonce, str) or len(nonce) != 64:
            raise ValueError('invalid peer challenge')
        signature = mac(key, 'request', nonce, method, path, body)
        conn.close()
        conn = http.client.HTTPConnection(host, port, timeout=6)
        conn.request(method, path, body=body, headers={'X-HollowBridge-Nonce': nonce, 'X-HollowBridge-Auth': signature, 'Content-Type': 'application/json'})
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('peer response too large')
        expected = mac(key, 'response', nonce, str(response.status), raw)
        if not hmac.compare_digest(response.getheader('X-HollowBridge-Auth', ''), expected):
            raise ValueError('peer response authentication failed')
        if not 200 <= response.status < 300:
            raise OSError('peer rejected request')
        return json.loads(raw)
    finally:
        conn.close()
