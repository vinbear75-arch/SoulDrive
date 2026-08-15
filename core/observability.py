import threading
import time
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeMetrics:
    started_at: float = field(default_factory=time.time)
    total_requests: int = 0
    unauthorized_requests: int = 0
    rejected_remote_requests: int = 0
    rate_limited_requests: int = 0
    rejected_body_requests: int = 0
    failed_requests: int = 0
    chat_requests: int = 0
    chat_failures: int = 0
    retrieval_failures: int = 0
    retrieval_runs: int = 0
    retrieval_skips: int = 0
    retrieval_corrections: int = 0
    context_gate_accepts: int = 0
    context_gate_retries: int = 0
    context_gate_rejects: int = 0
    last_context_gate_accepted_count: int = 0
    last_context_gate_rejected_count: int = 0
    memory_gate_accepts: int = 0
    memory_gate_skips: int = 0
    last_memory_gate_accepted_count: int = 0
    last_memory_gate_rejected_count: int = 0
    presentation_tasks_started: int = 0
    presentation_tasks_completed: int = 0
    presentation_tasks_failed: int = 0
    last_retrieval_ms: int = 0
    last_retrieval_candidate_count: int = 0
    last_retrieval_evidence_count: int = 0
    generation_failures: int = 0
    model_load_failures: int = 0
    indexing_runs: int = 0
    indexing_failures: int = 0
    last_indexing_ms: int = 0
    last_indexed_chunk_count: int = 0
    last_model_load_ms: int = 0
    total_latency_ms: int = 0
    last_error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record_request(self, elapsed_ms: int, status_code: int):
        with self._lock:
            self.total_requests += 1
            self.total_latency_ms += max(0, elapsed_ms)
            if status_code >= 500:
                self.failed_requests += 1

    def increment(self, name: str, amount: int = 1):
        with self._lock:
            current = getattr(self, name)
            setattr(self, name, current + amount)

    def record_error(self, message: str):
        with self._lock:
            self.last_error = _sanitize_error_message(message)[:500]

    def record_model_load(self, elapsed_ms: int):
        with self._lock:
            self.last_model_load_ms = max(0, elapsed_ms)

    def record_retrieval_run(self, elapsed_ms: int, candidate_count: int, evidence_count: int):
        with self._lock:
            self.retrieval_runs += 1
            self.last_retrieval_ms = max(0, elapsed_ms)
            self.last_retrieval_candidate_count = max(0, candidate_count)
            self.last_retrieval_evidence_count = max(0, evidence_count)

    def record_context_gate(self, decision: str, accepted_count: int, rejected_count: int):
        with self._lock:
            attribute = {
                "accept": "context_gate_accepts",
                "retry": "context_gate_retries",
                "reject": "context_gate_rejects",
            }.get(str(decision), "context_gate_rejects")
            setattr(self, attribute, getattr(self, attribute) + 1)
            self.last_context_gate_accepted_count = max(0, int(accepted_count))
            self.last_context_gate_rejected_count = max(0, int(rejected_count))

    def record_memory_gate(self, decision: str, accepted_count: int, rejected_count: int):
        with self._lock:
            attribute = "memory_gate_accepts" if decision == "accept" else "memory_gate_skips"
            setattr(self, attribute, getattr(self, attribute) + 1)
            self.last_memory_gate_accepted_count = max(0, int(accepted_count))
            self.last_memory_gate_rejected_count = max(0, int(rejected_count))

    def record_indexing_run(self, elapsed_ms: int, chunk_count: int, failed: bool = False):
        with self._lock:
            self.indexing_runs += 1
            if failed:
                self.indexing_failures += 1
            self.last_indexing_ms = max(0, elapsed_ms)
            self.last_indexed_chunk_count = max(0, chunk_count)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            average_latency = self.total_latency_ms / self.total_requests if self.total_requests else 0
            return {
                "uptime_seconds": int(time.time() - self.started_at),
                "total_requests": self.total_requests,
                "unauthorized_requests": self.unauthorized_requests,
                "rejected_remote_requests": self.rejected_remote_requests,
                "rate_limited_requests": self.rate_limited_requests,
                "rejected_body_requests": self.rejected_body_requests,
                "failed_requests": self.failed_requests,
                "chat_requests": self.chat_requests,
                "chat_failures": self.chat_failures,
                "retrieval_failures": self.retrieval_failures,
                "retrieval_runs": self.retrieval_runs,
                "retrieval_skips": self.retrieval_skips,
                "retrieval_corrections": self.retrieval_corrections,
                "context_gate_accepts": self.context_gate_accepts,
                "context_gate_retries": self.context_gate_retries,
                "context_gate_rejects": self.context_gate_rejects,
                "last_context_gate_accepted_count": self.last_context_gate_accepted_count,
                "last_context_gate_rejected_count": self.last_context_gate_rejected_count,
                "memory_gate_accepts": self.memory_gate_accepts,
                "memory_gate_skips": self.memory_gate_skips,
                "last_memory_gate_accepted_count": self.last_memory_gate_accepted_count,
                "last_memory_gate_rejected_count": self.last_memory_gate_rejected_count,
                "presentation_tasks_started": self.presentation_tasks_started,
                "presentation_tasks_completed": self.presentation_tasks_completed,
                "presentation_tasks_failed": self.presentation_tasks_failed,
                "last_retrieval_ms": self.last_retrieval_ms,
                "last_retrieval_candidate_count": self.last_retrieval_candidate_count,
                "last_retrieval_evidence_count": self.last_retrieval_evidence_count,
                "generation_failures": self.generation_failures,
                "model_load_failures": self.model_load_failures,
                "indexing_runs": self.indexing_runs,
                "indexing_failures": self.indexing_failures,
                "last_indexing_ms": self.last_indexing_ms,
                "last_indexed_chunk_count": self.last_indexed_chunk_count,
                "last_model_load_ms": self.last_model_load_ms,
                "average_latency_ms": round(average_latency, 2),
                "last_error": self.last_error,
            }


runtime_metrics = RuntimeMetrics()


WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[A-Z]:\\[^\s\r\n\t\"']+")
POSIX_PATH_PATTERN = re.compile(r"(?<!\w)/(?:[^\s\r\n\t\"']+/)+[^\s\r\n\t\"']+")
SENSITIVE_FIELD_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|hardware[_-]?sn|serial)\b\s*[:=]\s*[^\s\r\n\t\"']+"
)


def _sanitize_error_message(message: str) -> str:
    text = str(message or "")
    text = WINDOWS_PATH_PATTERN.sub("[local path]", text)
    text = POSIX_PATH_PATTERN.sub("[local path]", text)
    return SENSITIVE_FIELD_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)
