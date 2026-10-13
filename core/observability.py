import hashlib
import threading
import time
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
    chat_abandoned: int = 0
    chat_completed: int = 0
    chat_under_5s: int = 0
    last_chat_first_token_ms: int = 0
    last_chat_total_ms: int = 0
    total_chat_first_token_ms: int = 0
    total_chat_completion_ms: int = 0
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
    last_memory_gate_decision_reason: str | None = None
    learning_context_scope_mismatches: int = 0
    presentation_tasks_started: int = 0
    presentation_tasks_completed: int = 0
    presentation_tasks_failed: int = 0
    last_retrieval_ms: int = 0
    last_retrieval_candidate_count: int = 0
    last_retrieval_evidence_count: int = 0
    generation_failures: int = 0
    model_load_failures: int = 0
    audit_failures: int = 0
    indexing_runs: int = 0
    indexing_failures: int = 0
    last_indexing_ms: int = 0
    last_indexed_chunk_count: int = 0
    last_model_load_ms: int = 0
    total_latency_ms: int = 0
    last_error: str | None = None
    last_error_observation: dict[str, Any] | None = None
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
            self.last_error = "运行异常，请查看运行诊断。"
            self.last_error_observation = _error_observation(message)

    def record_model_load(self, elapsed_ms: int):
        with self._lock:
            self.last_model_load_ms = max(0, elapsed_ms)

    def record_chat_latency(self, first_token_ms: int, total_ms: int, *, success: bool):
        with self._lock:
            self.last_chat_first_token_ms = max(0, int(first_token_ms))
            self.last_chat_total_ms = max(0, int(total_ms))
            if not success:
                return
            self.chat_completed += 1
            self.total_chat_first_token_ms += self.last_chat_first_token_ms
            self.total_chat_completion_ms += self.last_chat_total_ms
            if self.last_chat_total_ms < 5000:
                self.chat_under_5s += 1

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

    def record_memory_gate(
        self,
        decision: str,
        accepted_count: int,
        rejected_count: int,
        decision_reason: str | None = None,
    ):
        with self._lock:
            attribute = "memory_gate_accepts" if decision == "accept" else "memory_gate_skips"
            setattr(self, attribute, getattr(self, attribute) + 1)
            self.last_memory_gate_accepted_count = max(0, int(accepted_count))
            self.last_memory_gate_rejected_count = max(0, int(rejected_count))
            normalized_reason = str(decision_reason or "").strip()
            self.last_memory_gate_decision_reason = normalized_reason[:64] or None

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
            average_chat_first_token = (
                self.total_chat_first_token_ms / self.chat_completed if self.chat_completed else 0
            )
            average_chat_completion = (
                self.total_chat_completion_ms / self.chat_completed if self.chat_completed else 0
            )
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
                "chat_abandoned": self.chat_abandoned,
                "chat_completed": self.chat_completed,
                "chat_under_5s": self.chat_under_5s,
                "chat_under_5s_rate": round(
                    self.chat_under_5s / self.chat_completed if self.chat_completed else 0,
                    6,
                ),
                "last_chat_first_token_ms": self.last_chat_first_token_ms,
                "last_chat_total_ms": self.last_chat_total_ms,
                "average_chat_first_token_ms": round(average_chat_first_token, 2),
                "average_chat_total_ms": round(average_chat_completion, 2),
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
                "last_memory_gate_decision_reason": self.last_memory_gate_decision_reason,
                "learning_context_scope_mismatches": self.learning_context_scope_mismatches,
                "presentation_tasks_started": self.presentation_tasks_started,
                "presentation_tasks_completed": self.presentation_tasks_completed,
                "presentation_tasks_failed": self.presentation_tasks_failed,
                "last_retrieval_ms": self.last_retrieval_ms,
                "last_retrieval_candidate_count": self.last_retrieval_candidate_count,
                "last_retrieval_evidence_count": self.last_retrieval_evidence_count,
                "generation_failures": self.generation_failures,
                "model_load_failures": self.model_load_failures,
                "audit_failures": self.audit_failures,
                "indexing_runs": self.indexing_runs,
                "indexing_failures": self.indexing_failures,
                "last_indexing_ms": self.last_indexing_ms,
                "last_indexed_chunk_count": self.last_indexed_chunk_count,
                "last_model_load_ms": self.last_model_load_ms,
                "average_latency_ms": round(average_latency, 2),
                "last_error": self.last_error,
                "last_error_observation": dict(self.last_error_observation or {}),
            }


def _error_observation(message: str) -> dict[str, Any]:
    text = str(message or "")
    return {
        "error_type": "UnknownError",
        "error_digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "error_chars": len(text),
    }


runtime_metrics = RuntimeMetrics()
