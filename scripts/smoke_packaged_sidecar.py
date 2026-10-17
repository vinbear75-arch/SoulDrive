"""Exercise an explicit Windows sidecar in a new, retained test directory only."""

import argparse
import csv
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from zipfile import ZipFile


def _exercise_confirmed_presentation(request, workspace: Path, body: dict, result: dict):
    project_id = body["project_id"]
    status, planned = request("/presentations/generate", body)
    assert status == 202, planned

    def wait_task(task_id):
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            code, task = request(f"/presentations/tasks/{task_id}?project_id={project_id}")
            assert code == 200, task
            if task["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                assert task["status"] == "completed", task
                return task
            time.sleep(0.5)
        raise TimeoutError(f"presentation task did not finish: {task_id}")

    plan = wait_task(planned["task_id"])
    result["plan"] = plan
    assert plan["result"]["phase"] == "outline_review"
    assert not list((workspace / "data/presentations").glob("*.pptx"))
    confirm_url = f"/presentations/tasks/{plan['task_id']}/confirm?project_id={project_id}"
    confirm_body = {"plan_digest": plan["result"]["plan_digest"]}
    status, generation = request(confirm_url, confirm_body)
    assert status == 202, generation
    assert request(confirm_url, confirm_body)[1]["task_id"] == generation["task_id"]
    generated = wait_task(generation["task_id"])
    result["generation"] = generated
    output = Path(generated["result"]["output_path"]).resolve(strict=True)
    assert output.is_relative_to(workspace) and output.suffix == ".pptx"
    with ZipFile(output) as archive:
        assert archive.testzip() is None and "ppt/presentation.xml" in archive.namelist()
    result["output_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()


def _exercise_project_flow(request, workspace: Path, output_dir: Path, result: dict):
    def step(name):
        result.setdefault("steps", []).append(name)
        print(json.dumps({"phase": name}, ensure_ascii=False), flush=True)

    step("create_projects_and_import")
    for project_id, value in (("flow-a", "-5%"), ("flow-b", "+9%")):
        status, payload = request("/projects", {"project_id": project_id, "name": project_id})
        assert status == 200, payload
        source = output_dir / "inputs" / project_id / "measurement.md"
        source.parent.mkdir(parents=True)
        source.write_text(
            f"# 工程链路验证记录\n\n## 测量结论\n\n本轮实验测量值为 {value}。\n\n"
            "此文件仅供隔离工作区的链路测试，不代表真实实验。\n",
            encoding="utf-8",
        )
        status, payload = request("/documents/import", {"project_id": project_id, "source_paths": [str(source)]})
        assert status == 200 and payload["imported_count"] == 1, payload
    step("index_real_models")
    status, indexing = request("/index/run", {})
    assert status == 200 or status == 202, indexing
    result["index_run_id"] = indexing["run_id"]
    while True:
        state = request("/runtime/status")[1]["indexing"]
        if state.get("run_id") == indexing["run_id"] and state.get("status") in {"idle", "completed", "failed", "blocked"}:
            result["indexing"] = state
            assert state["status"] in {"idle", "completed"}, state
            assert state["processed_files"] >= 2 and not state["failures"], state
            break
        time.sleep(2)
    documents = request("/documents/list")[1]["documents"]
    selected = {}
    for project_id in ("flow-a", "flow-b"):
        matches = [item for item in documents if item.get("project_id") == project_id]
        assert len(matches) == 1 and matches[0]["indexed"], matches
        selected[project_id] = matches[0]
    result["documents"] = selected

    step("index_diagnostic_identity")
    result["diagnostic_identity_checks"] = {}
    for project_id, document in selected.items():
        identities = {document["document_id"], f"secure_{document['document_id']}"}
        observations = [
            item for item in result["indexing"]["observability"]["documents"]
            if item.get("document_id") in identities and item.get("project_id") == project_id
        ]
        assert len(observations) == 1, (project_id, document, observations)
        observation = document["index_observation"]
        assert observation and all(observations[0].get(key) == value for key, value in observation.items())
        result["diagnostic_identity_checks"][project_id] = {
            "document_id": observations[0]["document_id"],
            "elapsed_ms": observation["elapsed_ms"], "matched": True,
        }

    step("project_chat_scope_and_answer")
    assert request("/projects/flow-a/sessions", {"session_id": "flow-chat", "title": "工程验证"})[0] == 200
    context = {
        "project_id": "flow-a", "document_ids": [selected["flow-a"]["document_id"]],
        "document_names": [selected["flow-a"]["name"]],
    }
    query = {"session_id": "flow-chat", "request_id": "flow-chat-request", "query": "本轮实验测量值是多少？请引用所选文档。", "active_context": context}
    wrong = {**query, "request_id": "flow-cross-project", "active_context": {
        **context, "document_ids": [selected["flow-b"]["document_id"]],
        "document_names": [selected["flow-b"]["name"]],
    }}
    assert request("/chat", wrong)[0] == 409
    status, answer = request("/chat", query, raw=True, timeout=900)
    result["answer"] = answer
    (output_dir / "answer.txt").write_text(answer, encoding="utf-8")
    assert status == 200
    prose = answer.split("```souldrive-evidence", 1)[0]
    assert re.search(r"[-−]\s*5\s*[%％]", prose) and "[E1]" in prose, prose
    assert "+9%" not in prose, prose
    evidence_match = re.search(r"```souldrive-evidence\s*\n(.*?)\n```", answer, re.S)
    assert evidence_match, answer
    evidence = json.loads(evidence_match.group(1))
    result["evidence"] = evidence
    assert evidence and all(item.get("project_id") == "flow-a" for item in evidence), evidence
    history_url = "/sessions/flow-chat/history?project_id=flow-a"
    history_status, before_replay = request(history_url)
    assert history_status == 200 and len(before_replay["history"]) == 2, before_replay
    persisted_answer = before_replay["history"][-1]["content"]
    assert persisted_answer and answer.endswith(persisted_answer)
    heartbeat_prefix = answer[:len(answer) - len(persisted_answer)]
    assert all(character == "\u200b" for character in heartbeat_prefix), repr(heartbeat_prefix)
    replay_status, replay_answer = request("/chat", query, raw=True, timeout=900)
    result["persisted_answer"] = persisted_answer
    result["replay_answer"] = replay_answer
    result["transport_heartbeat_count"] = len(heartbeat_prefix)
    assert replay_status == status and replay_answer == persisted_answer, (replay_status, replay_answer)
    assert request(history_url)[1]["history"] == before_replay["history"]
    audit_events = [json.loads(line) for line in (workspace / "audit/audit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(event["event_type"] == "rag.completed" for event in audit_events) == 1
    assert sum(event["event_type"] == "chat.replayed_idempotent" for event in audit_events) == 1

    step("confirmed_presentation")
    _exercise_confirmed_presentation(request, workspace, {
        "request_id": "flow-presentation", "project_id": "flow-a",
        "source_filenames": [selected["flow-a"]["name"]],
        "source_document_ids": [selected["flow-a"]["document_id"]],
        "title": "工程链路验证", "max_slides": 4, "open_in_wps": False,
    }, result)
    result["status"] = "pass"
    step("project_flow_complete")


def _exercise_experiment_flow(request, workspace: Path, output_dir: Path, result: dict):
    def step(name):
        result.setdefault("steps", []).append(name)
        print(json.dumps({"phase": name}, ensure_ascii=False), flush=True)

    step("import_metric_sources")
    for project_id in ("flow-a", "flow-b"):
        assert request("/projects", {"project_id": project_id, "name": project_id})[0] == 200
    fixtures = [
        ("baseline", "flow-a", "metrics.csv", "0.81", "2026-09-12T10:00:00+08:00", "config-a"),
        ("previous-repeat", "flow-a", "metrics-repeat.csv", "0.82", "2026-09-15T10:00:00+08:00", "config-a"),
        ("current", "flow-a", "metrics-current.csv", "0.86", "2026-10-12T10:00:00+08:00", "config-b"),
        ("foreign", "flow-b", "metrics.csv", "9.0", "2026-09-12T10:00:00+08:00", "config-a"),
    ]
    bodies, imports = {}, {}
    for role, project_id, name, value, performed_at, config in fixtures:
        source = output_dir / "inputs" / role / name
        source.parent.mkdir(parents=True)
        metadata = {"unit": "%", "direction": "higher", "definition_version": "metric-v1", "split": "test",
                    "dataset_version": "dataset-v1", "evaluation_script_ref": "eval-v1", "environment_ref": "env-a",
                    "performed_at": performed_at, "config_ref": config}
        with source.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["metric", "value", *metadata, "note"])
            writer.writerow(["accuracy", value, *metadata.values(), "仅供工程链路测试，非真实实验"])
        code, imported = request("/documents/import", {"project_id": project_id, "source_paths": [str(source)]})
        assert code == 200 and imported["imported_count"] == 1, imported
        imports[role] = imported
        bodies[role] = {"source_name": name, "experiment_name": "engineering-alignment", **metadata}
    result["imports"] = imports
    documents = request("/documents/list")[1]["documents"]
    selected = {}
    for role, project_id, name, *_ in fixtures:
        matches = [item for item in documents if item.get("project_id") == project_id and item["name"] == name]
        assert len(matches) == 1, matches
        selected[role] = matches[0]
    result["documents"] = selected

    step("metric_confirmation_and_identity")
    metrics_url = "/projects/flow-a/experiments/metrics"
    runs_url = "/projects/flow-a/experiments/runs"
    assert request(runs_url)[1]["runs"] == []
    code, preview = request(metrics_url, bodies["baseline"])
    result["metric_preview"] = {"http_status": code, "body": preview}
    assert code == 200 and preview["status"] == "confirmation_required", preview
    assert preview["plan"]["writes"] is False and request(runs_url)[1]["runs"] == []
    runs = {}
    for role in ("baseline", "previous-repeat", "current"):
        body = {**bodies[role], "source_document_id": selected[role]["document_id"]}
        code, source_preview = request(metrics_url, body)
        assert code == 200 and source_preview["plan"]["writes"] is False, source_preview
        body.update(source_version_id=source_preview["source_version_id"], confirmed=True)
        code, imported = request(metrics_url, body)
        assert code == 200 and imported["status"] == "imported", imported
        assert request(metrics_url, body)[1]["run_id"] == imported["run_id"]
        runs[role] = imported
    result["runs"] = runs
    assert len(request(runs_url)[1]["runs"]) == 3
    assert request(metrics_url, {**bodies["baseline"], "source_document_id": selected["foreign"]["document_id"], "confirmed": True})[0] == 409
    assert request(metrics_url, {**bodies["current"], "source_document_id": selected["baseline"]["document_id"], "confirmed": True})[0] == 409
    assert len(request(runs_url)[1]["runs"]) == 3

    step("explicit_experiment_comparison")
    compare_url = "/projects/flow-a/experiments/compare"
    card = request(compare_url, {"reference_at": "2026-10-13T12:00:00+08:00", "timezone_name": "Asia/Shanghai", "experiment_name": "engineering-alignment"})[1]
    result["selection_card"] = card
    assert card["status"] == "needs_confirmation" and card["previous_month"]["candidate_count"] == 2, card
    previous_ids = {item["run_id"] for item in card["previous_month"]["candidates"]}
    assert previous_ids == {runs[role]["run_id"] for role in ("baseline", "previous-repeat")}
    pair = {"baseline_run_id": runs["baseline"]["run_id"], "current_run_id": runs["current"]["run_id"]}
    comparison = request(compare_url, pair)[1]
    result["comparison"] = comparison
    assert comparison["status"] == "comparable" and comparison["changes"][0]["absolute_delta"] == 0.05, comparison
    assert request(compare_url, {"baseline_run_id": pair["baseline_run_id"], "current_run_id": pair["baseline_run_id"]})[1]["status"] == "not_comparable"
    repeated = request("/projects/flow-a/experiments/repeats/summary", {"run_ids": list(pair.values())})[1]
    assert repeated["status"] == "not_comparable" and repeated["metrics"] == [], repeated

    step("index_experiment_sources")
    code, indexing = request("/index/run", {})
    assert code in {200, 202}, indexing
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        state = request("/runtime/status")[1]["indexing"]
        if state.get("run_id") == indexing["run_id"] and state.get("status") in {"idle", "completed", "failed", "blocked"}:
            result["indexing"] = state
            assert state["status"] in {"idle", "completed"} and state["processed_files"] == 4 and not state["failures"], state
            break
        time.sleep(2)
    else:
        raise TimeoutError("experiment sources did not finish indexing")

    step("research_window_comparison")
    for session_id, workflow in (("flow-paper", "paper_review"), ("flow-experiment", "experiment_compare")):
        assert request("/projects/flow-a/sessions", {"session_id": session_id, "title": session_id, "workflow_id": workflow})[0] == 200
    context = {"project_id": "flow-a", **pair,
               "document_ids": [selected[role]["document_id"] for role in ("baseline", "current")],
               "document_names": [selected[role]["name"] for role in ("baseline", "current")]}
    query = "按账本已确认的单位，报告两次 accuracy 和绝对变化并引用证据；配置差异只能列为待验证因素。"
    chat_body = {"session_id": "flow-experiment", "request_id": "flow-experiment-request", "query": query, "active_context": context}
    assert request("/chat", {**chat_body, "session_id": "flow-paper", "request_id": "flow-paper-run-selection"})[0] == 409
    assert request("/projects/flow-b/sessions", {"session_id": "flow-foreign", "title": "foreign control", "workflow_id": "experiment_compare"})[0] == 200
    foreign_context = {"project_id": "flow-b", **pair}
    assert request("/chat", {**chat_body, "session_id": "flow-foreign", "request_id": "flow-foreign-run-selection", "active_context": foreign_context})[0] == 422
    specs = request("/multimodal/workflows?project_id=flow-a")[1]["workflows"]
    spec = next(item for item in specs if item["workflow_id"] == "experiment_compare")
    assert request("/projects/flow-a/playbooks/experiment_compare/approval", {"playbook_version": spec["playbook_version"], "approved": True})[0] == 200

    step("unconfirmed_material_preparation")
    assert request("/projects/flow-a/sessions", {"session_id": "flow-preparation", "title": "未绑定测量材料核对", "workflow_id": "experiment_compare"})[0] == 200
    material_context = {key: value for key, value in context.items() if key not in pair}
    preparation_body = {"session_id": "flow-preparation", "request_id": "flow-preparation-request",
                        "query": "对比这两份 accuracy 指标表，先检查比较口径再给出验证计划。", "active_context": material_context}
    code, preparation = request("/chat", preparation_body, raw=True, timeout=900)
    assert code == 200 and "指标口径确认" in preparation and "```souldrive-evidence" in preparation, preparation
    preparation_prose = preparation.split("```souldrive-evidence", 1)[0]
    assert "[M1]" not in preparation_prose and "| metric | value |" in preparation_prose, preparation
    preparation_events = [json.loads(line) for line in (workspace / "audit/audit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    preparation_reviews = [event for event in preparation_events if event["event_type"] == "rag.experiment_material_review_required"]
    assert len(preparation_reviews) == 1 and preparation_reviews[0]["payload"]["generation_attempts"] == 0
    assert preparation_reviews[0]["payload"]["side_effect"] == "none"
    preparation_history = request("/sessions/flow-preparation/history?project_id=flow-a")[1]["history"]
    assert len(preparation_history) == 2 and preparation.endswith(preparation_history[-1]["content"])
    assert request("/chat", preparation_body, raw=True, timeout=900)[1] == preparation_history[-1]["content"]
    assert len(request(runs_url)[1]["runs"]) == 3
    result["material_preparation"] = {"request": preparation_body, "persisted_answer": preparation_history[-1]["content"],
                                      "trace_id": preparation_reviews[0]["trace_id"], "generation_attempts": 0, "new_runs": 0}

    code, answer = request("/chat", chat_body, raw=True, timeout=900)
    result["answer"] = answer
    result["chat_request"] = chat_body
    audit_events = [json.loads(line) for line in (workspace / "audit/audit_log.jsonl").read_text(encoding="utf-8").splitlines()]
    result["answer_validation_events"] = [
        {"event_type": event["event_type"], "payload": event["payload"]}
        for event in audit_events if event["event_type"] in {
            "rag.answer_retry", "rag.refused_invalid_answer", "rag.answer_quality_fallback", "rag.completed",
        }
    ]
    (output_dir / "answer.txt").write_text(answer, encoding="utf-8")
    assert code == 200 and re.search(r"\[M\d+\]", answer), answer
    prose = answer.split("```souldrive-evidence", 1)[0]
    assert "0.05" in prose and "0.81" in prose and "0.86" in prose, prose
    match = re.search(r"```souldrive-evidence\s*\n(.*?)\n```", answer, re.S)
    assert match, answer
    evidence = json.loads(match.group(1))
    result["evidence"] = evidence
    metric_evidence = [item for item in evidence if str(item.get("id") or "").startswith("M")]
    assert metric_evidence and all(item.get("project_id") == "flow-a" for item in metric_evidence), metric_evidence
    serialized_metrics = json.dumps(metric_evidence, ensure_ascii=False)
    assert all(run_id in serialized_metrics for run_id in pair.values()), metric_evidence
    history_url = "/sessions/flow-experiment/history?project_id=flow-a"
    history = request(history_url)[1]["history"]
    assert len(history) == 2
    result["persisted_answer"] = history[-1]["content"]
    assert answer.endswith(result["persisted_answer"])
    assert request("/chat", chat_body, raw=True, timeout=900)[1] == result["persisted_answer"]

    step("comparison_presentation")
    _exercise_confirmed_presentation(request, workspace, {
        "request_id": "flow-experiment-presentation", "project_id": "flow-a",
        "source_filenames": context["document_names"], "source_document_ids": context["document_ids"],
        **pair,
        "title": "工程实验比较", "max_slides": 4, "open_in_wps": False,
    }, result)
    outline_text = json.dumps(result["plan"]["result"]["outline"], ensure_ascii=False)
    assert result["plan"]["result"]["experiment_comparison"] == comparison
    assert result["generation"]["result"]["experiment_comparison"] == comparison
    assert "0.05" in outline_text, "comparison conclusion is missing from the actual presentation outline"
    outline = result["plan"]["result"]["outline"]
    assert outline["outline_source"] == "experiment_ledger_extractive", outline
    assert "环境引用：基线 env-a；当前 env-a" in outline_text, outline
    assert "配置引用：基线 config-a；当前 config-b" in outline_text, outline
    assert "硬件环境发生变动" not in outline_text, outline
    result["status"] = "pass"
    step("experiment_flow_complete")


def _verify_experiment_reopen(request, result: dict):
    runs = request("/projects/flow-a/experiments/runs")[1]["runs"]
    assert {item["run_id"] for item in runs} == {item["run_id"] for item in result["runs"].values()}
    assert request("/chat", result["chat_request"], raw=True, timeout=900)[1] == result["persisted_answer"]
    preparation = result["material_preparation"]
    assert request("/chat", preparation["request"], raw=True, timeout=900)[1] == preparation["persisted_answer"]
    code, task = request(f"/presentations/tasks/{result['generation']['task_id']}?project_id=flow-a")
    assert code == 200 and task["status"] == "completed", task
    assert task["result"]["output_path"] == result["generation"]["result"]["output_path"]
    assert hashlib.sha256(Path(task["result"]["output_path"]).read_bytes()).hexdigest() == result["output_sha256"]
    result["reopened_result_verified"] = True


def _exercise_memory_flow(request, result: dict):
    """Independent fake preference lifecycle; not a business-quality evaluation."""
    def step(name):
        result.setdefault("steps", []).append(name)
        print(json.dumps({"phase": name}), flush=True)

    step("memory_projects_and_sessions")
    for project_id, policy in (("memory-a", "project_plus_global"), ("memory-b", "project_only")):
        assert request("/projects", {"project_id": project_id, "name": project_id, "memory_policy": policy})[0] == 200
    # General chat records the preference; a paper-review window correctly
    # requires indexed paper inputs and is not bypassed for this lifecycle test.
    for session_id, workflow in (("memory-source", None), ("memory-other", "experiment_compare")):
        assert request("/projects/memory-a/sessions", {"session_id": session_id, "title": session_id, "workflow_id": workflow})[0] == 200
    message = "以后请默认用中文回答。"
    step("memory_source_chat_requests")
    for request_id in ("older-same-text", "memory-source-request"):
        code, answer = request("/chat", {"query": message, "session_id": "memory-source", "request_id": request_id,
                                  "active_context": {"project_id": "memory-a"}}, raw=True, timeout=900)
        assert code == 200, (code, answer)
    records = request("/sessions/memory-source/history?project_id=memory-a")[1]["records"]
    original = next(item for item in records if item["role"] == "user" and item["request_id"] == "memory-source-request")
    body = {"message": message, "session_id": "memory-source", "project_id": "memory-a", "request_id": "memory-source-request"}
    step("memory_save_and_provenance")
    code, saved = request("/learning/memories/runtime", body)
    assert code == 200 and saved["status"] == "saved", saved
    memory_id = saved["memory_id"]
    code, replay = request("/learning/memories/runtime", body)
    assert code == 200 and replay["idempotent"] and replay["memory_id"] == memory_id
    assert request("/learning/memories/runtime", {**body, "message": "以后请默认用英文回答。"})[0] == 409
    assert request("/learning/memories/runtime", {**body, "project_id": "memory-b"})[0] == 409
    memory = next(item for item in request("/learning/memories?project_id=memory-a", timeout=120)[1]["memories"] if item["memory_id"] == memory_id)
    result.update(memory_id=memory_id, source_message_id=original["message_id"], source_sequence=original["sequence"], original=memory)
    assert memory["source_trace"]["message_ids"] == [original["message_id"]], memory
    assert memory["source_trace"]["message_sequences"] == [original["sequence"]], memory
    assert memory["scope"] == "global" and memory["project_id"] is None
    step("memory_search_scope_and_revision")
    search_body = {"query": message, "project_id": "memory-a", "workflow_id": "experiment_compare", "top_k_skill": 0}
    search = request("/learning/search", search_body, timeout=120)[1]
    assert any((item.get("memory_id") or item.get("asset_id")) == memory_id for item in search["memories"]), search
    result["semantic_projection"] = search["memory_retrieval"]
    assert request("/learning/search", {**search_body, "project_id": "memory-b"}, timeout=120)[1]["memories"] == []
    old_as_of = max(float(memory["updated_at"]), float(memory.get("valid_from") or 0))
    update = {"title": memory["title"], "content": "以后请默认用中文回答，并先给出结论。", "expected_updated_at": memory["updated_at"]}
    headers = {"Idempotency-Key": "memory-edit-once"}
    code, edited = request(f"/learning/memories/{memory_id}?project_id=memory-a", update, method="PATCH", extra_headers=headers, timeout=120)
    assert code == 200 and edited["memory"]["record_version"] > memory["record_version"], edited
    result["edit_storage_sync"] = edited["storage_sync"]
    assert edited["memory"]["source_trace"]["message_ids"] == [original["message_id"]]
    assert request(f"/learning/memories/{memory_id}?project_id=memory-a", update, method="PATCH", extra_headers=headers, timeout=120)[1]["idempotent_replay"]
    historical = request("/learning/search", {**search_body, "as_of": old_as_of}, timeout=120)[1]["memories"]
    assert any((item.get("memory_id") or item.get("asset_id")) == memory_id and item["content"] == memory["content"] for item in historical), historical
    revoke_url = f"/learning/memories/{memory_id}/revoke?project_id=memory-a"
    revoke_headers = {"Idempotency-Key": "memory-revoke-once"}
    assert request(revoke_url, {}, extra_headers=revoke_headers)[0] == 200
    assert request(revoke_url, {}, extra_headers=revoke_headers)[1]["idempotent_replay"]
    result.update(search_body=search_body, old_as_of=old_as_of, edited=edited["memory"], status="pass")
    _verify_memory_reopen(request, result)


def _verify_memory_reopen(request, result: dict):
    assert request("/learning/search", result["search_body"], timeout=120)[1]["memories"] == []
    historical = request("/learning/search", {**result["search_body"], "as_of": result["old_as_of"]}, timeout=120)[1]["memories"]
    old = next(item for item in historical if (item.get("memory_id") or item.get("asset_id")) == result["memory_id"])
    assert old["content"] == result["original"]["content"]
    assert old["source_trace"]["message_ids"] == [result["source_message_id"]]
    assert old["source_trace"]["message_sequences"] == [result["source_sequence"]]
    result["revoked_current_and_historical_provenance_verified"] = True


def run(executable: Path, output_dir: Path, *, project_flow: bool = False, experiment_flow: bool = False, memory_flow: bool = False, source: bool = False) -> dict:
    if not __debug__:
        raise ValueError("run smoke without Python assertion optimization")
    if sum((project_flow, experiment_flow, memory_flow)) > 1:
        raise ValueError("select one independent synthetic workflow")
    if os.name != "nt":
        raise ValueError("packaged sidecar smoke requires Windows")
    executable = executable.resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    source_hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in sorted((root / "core").glob("*.py"))} if source else {}
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    workspace = output_dir / "workspace"
    app_root = output_dir / "app"
    models = output_dir / "empty-models"
    models.mkdir()
    token, passphrase = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    opener = build_opener(ProxyHandler({}))
    report = {
        "schema_version": "souldrive_source_sidecar_smoke_v1" if source else "souldrive_packaged_sidecar_smoke_v1",
        "source_mode": source,
        "source_sha256": source_hashes,
        "status": "running",
        "executable": str(executable),
        "exe_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "workspace": str(workspace),
        "model_inference_tested": False,
        "desktop_or_installer_tested": False,
        "runs": [],
    }
    try:
        for run_number in (1, 2):
            with socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                port = reserved.getsockname()[1]
            base_url = f"http://127.0.0.1:{port}"

            def request(path, body=None, *, authenticated=True, raw=False, timeout=30, method=None, extra_headers=None):
                headers = {"X-SoulDrive-Token": token} if authenticated else {}
                headers.update(extra_headers or {})
                data = None
                if body is not None:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode("utf-8")
                try:
                    with opener.open(Request(base_url + path, data=data, headers=headers, method=method), timeout=timeout) as response:
                        return response.status, response.read().decode("utf-8") if raw else json.load(response)
                except HTTPError as error:
                    return error.code, json.load(error)

            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith("SOULDRIVE_") and key not in {"PYTHONPATH", "PYTHONHOME"}
            }
            environment.update({
                "SOULDRIVE_APP_ROOT": str(app_root),
                "SOULDRIVE_WORKSPACE": str(workspace),
                "SOULDRIVE_MODEL_DIR": str(models),
                "SOULDRIVE_WORKSPACE_MODE": "local",
                "SOULDRIVE_PC_ONLY": "1",
                "SOULDRIVE_WATCH_REMOVABLE": "0",
                "SOULDRIVE_REDIS_ENABLED": "0",
                "SOULDRIVE_POSTGRES_ENABLED": "0",
                "SOULDRIVE_API_HOST": "127.0.0.1",
                "SOULDRIVE_API_PORT": str(port),
                "SOULDRIVE_API_TOKEN": token,
                "SOULDRIVE_PARENT_PID": str(os.getpid()),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "ANONYMIZED_TELEMETRY": "False",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "PATH": os.pathsep.join((str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"])),
            })
            if source:
                environment["PATH"] = os.environ.get("PATH", "")
                environment["PYTHONPATH"] = str(root)
            if project_flow or experiment_flow or memory_flow:
                environment.update({
                    "SOULDRIVE_CHAT_PROVIDER": "ollama",
                    "SOULDRIVE_OLLAMA_MODEL": "qwen3.5:9b",
                    "SOULDRIVE_EMBEDDING_PROVIDER": "ollama",
                    "SOULDRIVE_OLLAMA_EMBEDDING_MODEL": "qwen3-embedding:4b",
                    "SOULDRIVE_PRESENTATION_RENDERER": "openxml",
                })
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            run_report = {"run": run_number, "port": port, "checks": [], "startup_ms": None}
            report["runs"].append(run_report)
            with (output_dir / f"process-{run_number}.log").open("xb") as process_log:
                process = subprocess.Popen(
                    [str(executable), "-m", "core.sidecar_runtime"] if source else [str(executable)],
                    cwd=root if source else output_dir, env=environment,
                    stdout=process_log, stderr=subprocess.STDOUT,
                    startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
                )
                run_report["pid"] = process.pid
                try:
                    started = time.monotonic()
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError(f"candidate exited before ready: {process.returncode}")
                        try:
                            status, security = request("/security/status")
                            if status == 200:
                                break
                        except (URLError, TimeoutError):
                            pass
                        if time.monotonic() - started > 120:
                            raise TimeoutError("candidate did not expose security status within 120 seconds")
                        time.sleep(0.2)
                    run_report["startup_ms"] = round((time.monotonic() - started) * 1000, 1)
                    state = json.loads((workspace / "runtime/runtime_state.json").read_text(encoding="utf-8"))
                    assert Path(state["workspace_path"]).resolve() == workspace, "unexpected workspace"
                    assert not security["software_unlocked"] and not security["development_bypass"]
                    assert security["crypto_initialized"] is (run_number == 2)
                    assert request("/documents/list", authenticated=False)[0] == 401
                    assert request("/documents/list")[0] == 423
                    run_report["checks"].append("cold_start_locked_and_token_required")

                    if run_number == 1:
                        assert request("/security/init", {"passphrase": passphrase, "acknowledge_no_recovery": False})[0] == 400
                        assert not (workspace / "config/keystore.json").exists()
                        status, initialized = request("/security/init", {"passphrase": passphrase, "acknowledge_no_recovery": True})
                        assert status == 200 and initialized["software_unlocked"]
                        assert (workspace / "config/keystore.json").is_file()
                        run_report["checks"].append("explicit_acknowledged_initialization")
                        if project_flow:
                            report["project_flow"] = {"status": "running"}
                            _exercise_project_flow(request, workspace, output_dir, report["project_flow"])
                            report["model_inference_tested"] = True
                        if experiment_flow:
                            report["experiment_flow"] = {"status": "running"}
                            _exercise_experiment_flow(request, workspace, output_dir, report["experiment_flow"])
                            report["model_inference_tested"] = True
                        if memory_flow:
                            report["memory_flow"] = {"status": "running"}
                            _exercise_memory_flow(request, report["memory_flow"])
                            report["model_inference_tested"] = bool(
                                report["memory_flow"]["semantic_projection"].get("semantic_count")
                                or any(item.get("vector_requested") for item in report["memory_flow"]["edit_storage_sync"].get("items", []))
                            )
                        assert request("/security/lock", {})[0] == 200

                    assert request("/security/unlock", {"passphrase": passphrase + "-wrong"})[0] == 403
                    assert not request("/security/status")[1]["software_unlocked"]
                    assert request("/security/unlock", {"passphrase": passphrase})[1]["software_unlocked"]
                    assert request("/documents/list")[0] == 200
                    if experiment_flow and run_number == 2:
                        _verify_experiment_reopen(request, report["experiment_flow"])
                    if memory_flow and run_number == 2:
                        _verify_memory_reopen(request, report["memory_flow"])
                        report["memory_flow"]["reopened_result_verified"] = True
                    assert not request("/security/lock", {})[1]["software_unlocked"]
                    run_report["checks"].append("wrong_passphrase_rejected_correct_unlock_and_relock")
                    assert request("/shutdown", {})[0] == 200
                    assert process.wait(timeout=30) == 0
                    run_report["checks"].append("graceful_shutdown")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=10)
                    run_report["exit_code"] = process.returncode
        if source:
            assert source_hashes == {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in source_hashes}
        report["status"] = "pass"
    except Exception as error:
        report["status"] = "fail"
        report["error_type"] = type(error).__name__
        report["error"] = str(error)[:1000]
    # A later answer/PPT failure does not erase a completed fresh-model index run.
    report["model_inference_tested"] = bool(report["model_inference_tested"] or any(
        (report.get(flow) or {}).get("indexing", {}).get("processed_files", 0) > 0
        for flow in ("project_flow", "experiment_flow")
    ))
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--executable", type=Path)
    target.add_argument("--source", action="store_true", help="Run the current source with this Python interpreter, not a packaged executable")
    parser.add_argument("--output-dir", type=Path, required=True)
    flows = parser.add_mutually_exclusive_group()
    flows.add_argument("--project-flow", action="store_true")
    flows.add_argument("--experiment-flow", action="store_true")
    flows.add_argument("--memory-flow", action="store_true")
    args = parser.parse_args()
    report = run(Path(sys.executable) if args.source else args.executable, args.output_dir,
                 project_flow=args.project_flow, experiment_flow=args.experiment_flow, memory_flow=args.memory_flow, source=args.source)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
