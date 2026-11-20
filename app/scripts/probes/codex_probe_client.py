"""Synchronous JSON-RPC peer for the local stdio app-server probes."""

import json


class ProbeClient:
    def __init__(self, proc):
        if proc.stdin is None or proc.stdout is None:
            raise ValueError("The probe requires stdio pipes")
        self.writer = proc.stdin
        self.reader = proc.stdout
        self.seq = 0
        self.notices = []
        self.denied = []

    def send(self, msg):
        print(json.dumps(msg), file=self.writer, flush=True)

    def receive(self):
        line = self.reader.readline()
        if not line:
            raise RuntimeError("app-server closed")
        msg = json.loads(line)
        if "method" in msg:
            if "id" in msg:
                self.denied.append(msg["method"])
                self.send(
                    {
                        "id": msg["id"],
                        "error": {
                            "code": -32601,
                            "message": "No interactive tools in this probe",
                        },
                    }
                )
            else:
                self.notices.append(msg)
        return msg

    def call(self, method, params):
        self.seq += 1
        self.send({"id": self.seq, "method": method, "params": params})
        while True:
            reply = self.receive()
            if "method" not in reply and reply.get("id") == self.seq:
                assert "result" in reply, reply
                return reply["result"]

    def initialize(self, name):
        self.call(
            "initialize",
            {
                "clientInfo": {"name": name, "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self.send({"method": "initialized"})
