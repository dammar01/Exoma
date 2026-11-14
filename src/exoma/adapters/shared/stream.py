"""Provider-aware observations: output is not evidence of work progress."""
import json
import re
import threading
import time

from exoma.core.evidence.provider_stream import DiagnosticSink
from exoma.utils.redact import redact_value

MAX_EVENT = 32768
_SENSITIVE = re.compile(r"(?i)^(?:api[_-]?key|token|access[_-]?token|password|passwd|secret|authorization|cookie)$")


def _safe(value):
    if isinstance(value, dict):
        value = {key: "[REDACTED]" if _SENSITIVE.fullmatch(str(key)) else _safe(child)
                 for key, child in value.items()}
    elif isinstance(value, list):
        value = [_safe(child) for child in value]
    return redact_value(value)[0]


class StreamTracker:
    def __init__(self, provider, directory=None, clock=time.monotonic):
        self.provider, self.clock = provider, clock
        self.started = self.output_at = self.work_at = clock()
        self.work_events = self.sequence = self.retry_count = self.dropped = 0
        self.capable = False
        self.diagnostic = None
        self.terminal_error = False
        self.first_error_at = self.last_error_at = None
        self.pending = {"stdout": "", "stderr": ""}
        self.discard = {"stdout": False, "stderr": False}
        self.lock = threading.Lock()
        self.sink = DiagnosticSink(directory)

    def feed(self, stream, fragment):
        with self.lock:
            self.output_at = self.clock()
            # A readline size limit can split JSON or credentials. Never persist fragments.
            text = self.pending[stream] + fragment
            if len(text) > MAX_EVENT:
                self.discard[stream] = True
                self.dropped += 1
            if not fragment.endswith("\n"):
                self.pending[stream] = "" if self.discard[stream] else text
                return
            self.pending[stream] = ""
            if self.discard[stream]:
                self.discard[stream] = False
                return
            try:
                self._event(stream, text)
            except (ValueError, TypeError, AttributeError, RecursionError):
                self.dropped += 1

    def _event(self, stream, text):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            value = None
        if not isinstance(value, dict):
            value = {"type": "unstructured", "text": text}
        kind = str(value.get("type") or value.get("event") or "unknown")
        work = False
        error = kind in {"error", "turn.failed", "response.failed"}
        if self.provider == "codex":
            self.capable |= kind in {"thread.started", "turn.started", "item.started", "item.completed", "turn.completed"}
            item = value.get("item") or {}
            work = kind in {"item.started", "item.completed"} and isinstance(item, dict) and item.get("type") in {
                "agent_message", "command_execution", "file_change", "mcp_tool_call", "web_search", "reasoning"}
        elif self.provider == "claude":
            self.capable |= kind in {"system", "assistant", "result", "stream_event"}
            work = kind == "assistant" and bool((value.get("message") or {}).get("content"))
            error |= kind == "result" and value.get("is_error") is True
        elif self.provider == "opencode":
            self.capable |= kind in {"step_start", "step_finish", "text", "reasoning", "tool_use", "error"}
            work = kind in {"step_finish", "text", "reasoning", "tool_use"}
        elif self.provider == "agy":
            update = value.get("step_update")
            work = isinstance(update, dict) and update.get("step_type") in {"agent_response", "tool_call", "tool_result"}
            self.capable |= kind == "result" or work
        # Unknown provider/event schemas retain output-idle fallback, never invent progress.
        now = self.clock()
        if work and not error:
            self.work_at = now
            self.work_events += 1
        blob = text.lower() if stream == "stderr" or kind in {"error", "turn.failed", "response.failed", "unstructured"} else ""
        diagnostic = "provider_error" if error else None
        if any(term in blob for term in ("retry", "reconnect", "rate limit", "websocket", "stream disconnected")):
            diagnostic = "retry_or_transport_error"
            self.retry_count += 1
        if "10013" in blob or "forbidden by its access permissions" in blob:
            diagnostic = "network_denied"
        if diagnostic:
            self.diagnostic = diagnostic
            self.first_error_at = self.first_error_at or time.time()
            self.last_error_at = time.time()
        self.terminal_error |= ((self.provider == "codex" and kind == "turn.failed") or
                                (self.provider == "claude" and kind == "result" and value.get("is_error") is True))
        self.sequence += 1
        # Redact decoded JSON before serialization (including escaped credential values).
        self.sink.emit(_safe({"sequence": self.sequence, "at": time.time(), "provider": self.provider,
                              "stream": stream, "event_type": kind, "work": work,
                              "diagnostic": diagnostic, "payload": value}))

    def snapshot(self):
        with self.lock:
            now = self.clock()
            return {"progress_schema": 1, "semantic_progress_supported": self.capable,
                    "work_idle_seconds": round(now - self.work_at, 1),
                    "work_events": self.work_events, "diagnostic": self.diagnostic,
                    "terminal_error": self.terminal_error,
                    "first_error_at": self.first_error_at, "last_error_at": self.last_error_at,
                    "retry_count": self.retry_count, "diagnostics": {**self.sink.meta(), "oversized_events": self.dropped}}

    def close(self):
        with self.lock:
            for stream in self.pending:
                # Incomplete final fragments have no trustworthy redaction boundary.
                if self.pending[stream]:
                    self.dropped += 1
                    self.pending[stream] = ""
        self.sink.close()
