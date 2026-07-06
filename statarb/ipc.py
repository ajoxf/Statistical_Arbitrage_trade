"""Newline-delimited JSON over TCP — the wire between coordinator and
leg runners. Localhost only; no credentials ever cross this link."""

import json
import socket


class JsonLineSocket:
    def __init__(self, sock):
        self.sock = sock
        self.file = sock.makefile('rwb')

    def send(self, obj):
        self.file.write(json.dumps(obj).encode('utf-8') + b'\n')
        self.file.flush()

    def recv(self):
        line = self.file.readline()
        if not line:
            return None
        return json.loads(line.decode('utf-8'))

    def request(self, obj):
        self.send(obj)
        return self.recv()

    def close(self):
        try:
            self.file.close()
        finally:
            self.sock.close()


def connect(host, port, timeout=5.0):
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    return JsonLineSocket(sock)


def parse_endpoint(endpoint):
    """'127.0.0.1:9101' -> ('127.0.0.1', 9101)"""
    host, _, port = endpoint.rpartition(':')
    return host or '127.0.0.1', int(port)
