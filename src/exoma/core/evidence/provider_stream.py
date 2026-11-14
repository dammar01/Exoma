"""Bounded diagnostic storage; never blocks provider pipe drainage."""
import json
from pathlib import Path
import queue
import threading
import uuid


class DiagnosticSink:
    def __init__(self, directory=None, byte_limit=2 * 1024 * 1024):
        self.path = Path(directory) / ("provider-" + uuid.uuid4().hex) if directory else None
        self.limit = max(65536, int(byte_limit)) // 2
        self.queue = queue.Queue(maxsize=128)
        self.dropped = 0
        self.truncated = False
        self.error = None
        self.stop = threading.Event()
        self.thread = None
        if self.path:
            self.thread = threading.Thread(target=self._write, daemon=True)
            self.thread.start()

    def emit(self, event):
        if not self.thread or self.error:
            return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _write(self):
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            sizes = {"head": 0, "tail": 0}
            first_error = False
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    event = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                line = (json.dumps(event, ensure_ascii=True, allow_nan=False) + "\n").encode()
                if len(line) > self.limit:
                    self.dropped += 1
                    continue
                if event.get("diagnostic") and not first_error:
                    (self.path / "first-error.jsonl").write_bytes(line)
                    first_error = True
                name = "head" if sizes["head"] + len(line) <= self.limit else "tail"
                mode = "ab"
                if sizes[name] + len(line) > self.limit:
                    mode, sizes[name], self.truncated = "wb", 0, True
                with (self.path / (name + ".jsonl")).open(mode) as stream:
                    stream.write(line)
                    stream.flush()
                sizes[name] += len(line)
        except (OSError, ValueError, TypeError) as exc:
            # No exception text: disk/path errors may contain credential-shaped names.
            self.error = type(exc).__name__

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                self.error = "diagnostic_flush_timeout"

    def meta(self):
        return {"path": str(self.path) if self.path else None, "dropped": self.dropped,
                "truncated": self.truncated, "error": self.error}
