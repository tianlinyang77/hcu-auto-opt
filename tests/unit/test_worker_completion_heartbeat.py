# Copyright (c) 2026 Hygon Information Technology Co., Ltd.

import threading

from hcuopt.domain.enums import WorkerType
from hcuopt.workers.sdk import Worker


class Handler:
    def __init__(self):
        self.entered = threading.Event()
        self.cleanup_count = 0

    def handle(self, job_type, payload):
        assert self.entered.wait(2)
        return {"done": True}

    def cleanup(self, job_type, payload):
        self.cleanup_count += 1
        return {}


class Client:
    def __init__(self, handler, *, lose_lease=False):
        self.handler = handler
        self.background_thread = None
        self.completed = False
        self.failed = False
        self.lose_lease = lose_lease

    def register(self, *args):
        pass

    def claim(self, worker_id):
        return {"job_id": "test", "job_type": "source_prepare", "attempts": 1,
                "payload": {}, "claim_token": "test"}

    def heartbeat(self, worker_id, job):
        assert not self.completed, "terminal jobs must never receive a heartbeat"
        if threading.current_thread() is not threading.main_thread():
            self.background_thread = threading.current_thread()
            self.handler.entered.set()
            if self.lose_lease:
                raise RuntimeError("lost lease")

    def complete(self, job, result):
        assert self.background_thread is not None
        assert not self.background_thread.is_alive()
        self.completed = True

    def fail(self, *args):
        self.failed = True


def run(lose_lease):
    handler = Handler()
    worker = Worker("fixture", WorkerType.BUILD, "http://127.0.0.1:1",
                    handlers=handler, heartbeat_seconds=.001)
    worker.client.client.close()
    client = Client(handler, lose_lease=lose_lease)
    worker.client = client
    return worker.run_once(), client, handler


def test_heartbeat_quiesced_before_completion():
    ok, client, handler = run(False)
    assert ok and client.completed and not client.failed
    assert handler.cleanup_count == 0


def test_real_lease_loss_still_fails_and_cleans_up():
    ok, client, handler = run(True)
    assert not ok and client.failed and not client.completed
    assert handler.cleanup_count >= 1
