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
    code, task = request(f"/presentations/tasks/{result['generation']['task_id']}?project_id=flow-a")
    assert code == 200 and task["status"] == "completed", task
    assert task["result"]["output_path"] == result["generation"]["result"]["output_path"]
    assert hashlib.sha256(Path(task["result"]["output_path"]).read_bytes()).hexdigest() == result["output_sha256"]
    result["reopened_result_verified"] = True


def run(executable: Path, output_dir: Path, *, project_flow: bool = False, experiment_flow: bool = False) -> dict:
    if not __debug__:
        raise ValueError("run smoke without Python assertion optimization")
    if project_flow and experiment_flow:
        raise ValueError("select one independent synthetic workflow")
    if os.name != "nt":
        raise ValueError("packaged sidecar smoke requires Windows")
    executable = executable.resolve(strict=True)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    workspace = output_dir / "workspace"
    app_root = output_dir / "app"
    models = output_dir / "empty-models"
    models.mkdir()
    token, passphrase = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    opener = build_opener(ProxyHandler({}))
    report = {
        "schema_version": "souldrive_packaged_sidecar_smoke_v1",
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

            def request(path, body=None, *, authenticated=True, raw=False, timeout=30):
                headers = {"X-SoulDrive-Token": token} if authenticated else {}
                data = None
                if body is not None:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode("utf-8")
                try:
                    with opener.open(Request(base_url + path, data=data, headers=headers), timeout=timeout) as response:
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
            if project_flow or experiment_flow:
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
                    [str(executable)], cwd=output_dir, env=environment,
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
                        assert request("/security/lock", {})[0] == 200

                    assert request("/security/unlock", {"passphrase": passphrase + "-wrong"})[0] == 403
                    assert not request("/security/status")[1]["software_unlocked"]
                    assert request("/security/unlock", {"passphrase": passphrase})[1]["software_unlocked"]
                    assert request("/documents/list")[0] == 200
                    if experiment_flow and run_number == 2:
                        _verify_experiment_reopen(request, report["experiment_flow"])
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
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    flows = parser.add_mutually_exclusive_group()
    flows.add_argument("--project-flow", action="store_true")
    flows.add_argument("--experiment-flow", action="store_true")
    args = parser.parse_args()
    report = run(args.executable, args.output_dir, project_flow=args.project_flow, experiment_flow=args.experiment_flow)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
