import unittest

from core.episodic_view import build_episodic_view, select_episodic_events_for_query


class EpisodicViewTests(unittest.TestCase):
    def test_build_episodic_view_merges_sources_with_stable_identity(self):
        result = build_episodic_view(
            project_id="project-a",
            sessions=[
                {
                    "session_id": "session-b",
                    "project_id": "project-a",
                    "title": "实验窗口",
                    "workflow_id": "experiment_compare",
                    "created_at": "2026-09-10T09:00:00+08:00",
                    "updated_at": "2026-09-12T10:00:00+08:00",
                },
                {
                    "session_id": "foreign-session",
                    "project_id": "project-b",
                    "title": "不应出现",
                    "updated_at": "2026-09-13T10:00:00+08:00",
                },
            ],
            experiment_runs=[
                {
                    "run_id": "run-a",
                    "project_id": "project-a",
                    "experiment_name": "baseline",
                    "performed_at": "2026-09-11T10:00:00+08:00",
                    "dataset_version": "v1",
                },
            ],
            decisions=[
                {
                    "decision_id": "decision-a",
                    "project_id": "project-a",
                    "statement": "先确认实验口径。",
                    "decided_at": "2026-09-12T11:00:00+08:00",
                },
            ],
            durable_tasks=[
                {
                    "task_id": "task-a",
                    "project_id": "project-a",
                    "kind": "presentation.generate",
                    "status": "failed",
                    "updated_at": "2026-09-12T12:00:00+08:00",
                    "current_step": "render",
                },
            ],
        )

        self.assertEqual(result["schema_version"], "souldrive_episodic_view_v0.1")
        self.assertEqual(result["project_id"], "project-a")
        self.assertEqual(
            [item["event_id"] for item in result["events"]],
            [
                "durable_task:task-a",
                "decision:decision-a",
                "conversation_session:session-b",
                "experiment_run:run-a",
            ],
        )
        self.assertEqual(
            result["events"][2]["workflow_id"],
            "experiment_compare",
        )
        self.assertNotIn("foreign-session", str(result))
        self.assertEqual(result["scope_rejected_count"], 1)
        self.assertEqual(result["source_counts"], {
            "durable_task": 1,
            "decision": 1,
            "conversation_session": 1,
            "experiment_run": 1,
        })

    def test_build_episodic_view_supports_as_of_and_requires_project_scope(self):
        result = build_episodic_view(
            project_id="project-a",
            sessions=[],
            experiment_runs=[
                {
                    "run_id": "old-run",
                    "project_id": "project-a",
                    "experiment_name": "old",
                    "performed_at": "2026-09-01T10:00:00+08:00",
                },
                {
                    "run_id": "new-run",
                    "project_id": "project-a",
                    "experiment_name": "new",
                    "performed_at": "2026-09-12T10:00:00+08:00",
                },
            ],
            decisions=[],
            durable_tasks=[
                {
                    "task_id": "global-task",
                    "kind": "presentation.generate",
                    "status": "completed",
                    "updated_at": "2026-09-01T11:00:00+08:00",
                },
            ],
            as_of="2026-09-05T00:00:00+08:00",
        )

        self.assertEqual(
            [item["event_id"] for item in result["events"]],
            ["experiment_run:old-run"],
        )

    def test_build_episodic_view_rejects_invalid_as_of(self):
        with self.assertRaises(ValueError):
            build_episodic_view(
                project_id="project-a",
                sessions=[],
                experiment_runs=[],
                decisions=[],
                durable_tasks=[],
                as_of="not-a-timestamp",
            )

    def test_build_episodic_view_emits_stable_digest_for_reordered_sources(self):
        kwargs = {
            "project_id": "project-a",
            "sessions": [{
                "session_id": "session-a",
                "project_id": "project-a",
                "title": "论文窗口",
                "updated_at": "2026-09-12T10:00:00+08:00",
            }],
            "experiment_runs": [{
                "run_id": "run-a",
                "project_id": "project-a",
                "experiment_name": "baseline",
                "performed_at": "2026-09-11T10:00:00+08:00",
            }],
            "decisions": [{
                "decision_id": "decision-a",
                "project_id": "project-a",
                "statement": "保留来源证据。",
                "decided_at": "2026-09-10T10:00:00+08:00",
            }],
            "durable_tasks": [],
        }

        first = build_episodic_view(**kwargs)
        second = build_episodic_view(
            **{
                **kwargs,
                "sessions": list(reversed(kwargs["sessions"])),
                "experiment_runs": list(reversed(kwargs["experiment_runs"])),
                "decisions": list(reversed(kwargs["decisions"])),
            }
        )

        self.assertRegex(first["view_digest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(first["view_digest"], second["view_digest"])

    def test_build_episodic_view_digest_changes_when_event_changes(self):
        common = {
            "project_id": "project-a",
            "sessions": [],
            "experiment_runs": [],
            "durable_tasks": [],
        }
        first = build_episodic_view(
            **common,
            decisions=[{
                "decision_id": "decision-a",
                "project_id": "project-a",
                "statement": "保留来源证据。",
                "decided_at": "2026-09-10T10:00:00+08:00",
            }],
        )
        changed = build_episodic_view(
            **common,
            decisions=[{
                "decision_id": "decision-a",
                "project_id": "project-a",
                "statement": "保留来源和版本证据。",
                "decided_at": "2026-09-10T10:00:00+08:00",
            }],
        )

        self.assertNotEqual(first["view_digest"], changed["view_digest"])

    def test_build_episodic_view_rejects_unscoped_history_records(self):
        result = build_episodic_view(
            project_id="project-a",
            sessions=[],
            experiment_runs=[
                {
                    "run_id": "unscoped-run",
                    "experiment_name": "不能猜项目",
                    "performed_at": "2026-09-12T10:00:00+08:00",
                },
                {
                    "run_id": "foreign-run",
                    "project_id": "project-b",
                    "experiment_name": "其他项目",
                    "performed_at": "2026-09-12T09:00:00+08:00",
                },
            ],
            decisions=[
                {
                    "decision_id": "unscoped-decision",
                    "statement": "不能猜项目",
                    "decided_at": "2026-09-12T08:00:00+08:00",
                },
                {
                    "decision_id": "foreign-decision",
                    "project_id": "project-b",
                    "statement": "其他项目",
                    "decided_at": "2026-09-12T07:00:00+08:00",
                },
            ],
            durable_tasks=[],
        )

        self.assertEqual(result["events"], [])
        self.assertEqual(result["scope_rejected_count"], 4)

    def test_build_episodic_view_includes_temporal_project_tasks_for_meeting_window(self):
        result = build_episodic_view(
            project_id="project-a",
            sessions=[],
            experiment_runs=[],
            decisions=[],
            durable_tasks=[],
            project_tasks=[
                {
                    "task_id": "meeting-task",
                    "project_id": "project-a",
                    "title": "确认实验负责人",
                    "status": "open",
                    "due_at": "2026-09-15T18:00:00+08:00",
                    "created_at": "2026-09-12T12:00:00+08:00",
                    "updated_at": "2026-09-12T13:00:00+08:00",
                },
                {
                    "task_id": "legacy-task",
                    "project_id": "project-a",
                    "title": "缺少活动时间的旧任务",
                    "status": "open",
                    "due_at": "2026-09-14T18:00:00+08:00",
                },
                {
                    "task_id": "foreign-task",
                    "project_id": "project-b",
                    "title": "其他项目任务",
                    "status": "open",
                    "updated_at": "2026-09-12T14:00:00+08:00",
                },
            ],
        )

        self.assertEqual(
            [item["event_id"] for item in result["events"]],
            ["project_task:meeting-task"],
        )
        self.assertEqual(result["invalid_event_count"], 1)
        self.assertEqual(result["scope_rejected_count"], 1)
        meeting = select_episodic_events_for_query(
            result,
            query="请回顾上次会议的行动项和负责人",
            workflow_id="meeting_actions",
        )
        self.assertEqual(
            [item["event_id"] for item in meeting["events"]],
            ["project_task:meeting-task"],
        )
        self.assertEqual(meeting["events"][0]["due_at"], "2026-09-15T18:00:00+08:00")

    def test_project_tasks_are_not_available_to_experiment_window_without_workflow_binding(self):
        view = build_episodic_view(
            project_id="project-a",
            sessions=[],
            experiment_runs=[],
            decisions=[],
            durable_tasks=[],
            project_tasks=[
                {
                    "task_id": "meeting-task",
                    "project_id": "project-a",
                    "title": "确认实验负责人",
                    "status": "open",
                    "created_at": "2026-09-12T12:00:00+08:00",
                    "updated_at": "2026-09-12T13:00:00+08:00",
                },
            ],
        )

        selected = select_episodic_events_for_query(
            view,
            query="请回顾上次实验的验证任务",
            workflow_id="experiment_compare",
        )
        self.assertEqual(selected["events"], [])

    def test_select_episodic_events_is_temporal_and_workflow_scoped(self):
        view = build_episodic_view(
            project_id="project-a",
            sessions=[
                {
                    "session_id": "meeting-session",
                    "project_id": "project-a",
                    "title": "会议窗口",
                    "workflow_id": "meeting_actions",
                    "updated_at": "2026-09-12T12:00:00+08:00",
                },
            ],
            experiment_runs=[
                {
                    "run_id": "run-a",
                    "project_id": "project-a",
                    "experiment_name": "baseline",
                    "performed_at": "2026-09-11T10:00:00+08:00",
                    "dataset_version": "v1",
                    "workflow_id": "experiment_compare",
                },
            ],
            decisions=[
                {
                    "decision_id": "decision-a",
                    "project_id": "project-a",
                    "statement": "保留当前评测口径，下一轮补做验证实验。",
                    "decided_at": "2026-09-12T11:00:00+08:00",
                },
            ],
            durable_tasks=[],
        )

        meeting = select_episodic_events_for_query(
            view,
            query="请回顾上次会议的决策和行动项",
            workflow_id="meeting_actions",
        )
        self.assertEqual(
            [item["event_id"] for item in meeting["events"]],
            ["decision:decision-a", "conversation_session:meeting-session"],
        )
        self.assertEqual(meeting["workflow_id"], "meeting_actions")
        self.assertEqual(meeting["scope"]["project_id"], "project-a")

        experiment = select_episodic_events_for_query(
            view,
            query="请回顾上次实验运行和数据集版本",
            workflow_id="experiment_compare",
        )
        self.assertEqual(
            [item["event_id"] for item in experiment["events"]],
            ["experiment_run:run-a"],
        )

        self.assertEqual(
            select_episodic_events_for_query(
                view,
                query="请精读当前论文并整理核心方法",
                workflow_id="paper_review",
            )["events"],
            [],
        )
        self.assertEqual(
            select_episodic_events_for_query(
                view,
                query="请把本轮记录整理为行动项",
                workflow_id="meeting_actions",
            )["events"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
