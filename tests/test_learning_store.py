import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.learning_store import LearningStore
from core.workspace import SoulDriveWorkspace
from core.workspace_crypto import initialize_keystore, unlock_keystore


class LearningStoreTests(unittest.TestCase):
    def _store(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        workspace = SoulDriveWorkspace.from_drive(temp_dir.name).ensure()
        initialize_keystore(workspace, "correct horse battery staple")
        keys = unlock_keystore(workspace, "correct horse battery staple")
        store = LearningStore(workspace, keys)
        self.addCleanup(store.close)
        return workspace, store

    def test_workspace_exposes_learning_store_path(self):
        workspace, _store = self._store()
        normalized = workspace.learning_store_path.replace("\\", "/")
        self.assertTrue(normalized.endswith("data/learning.sqlite"))

    def test_get_skill_reads_only_the_requested_active_skill(self):
        _workspace, store = self._store()
        store.sync_project_skill({
            "title": "requested-skill",
            "content": "Use the requested skill.",
            "source_path": "requested-skill/SKILL.md",
            "source_digest": "requested-digest",
        })

        [created] = store.list_skills()
        loaded = store.get_skill(created["skill_id"])

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["skill_id"], created["skill_id"])
        self.assertIsNone(store.get_skill("skill_missing"))

    def test_new_store_lists_no_pending_reviews_or_assets(self):
        _workspace, store = self._store()
        self.assertEqual(store.list_pending_reviews(), [])
        self.assertEqual(store.list_memories(), [])
        self.assertEqual(store.list_skills(), [])

    def test_memory_payload_is_encrypted_and_readable(self):
        workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = store._encrypt_payload(
            {"title": "关闭前审核", "content": "保存可复用记忆"},
            table="memories",
            pk_name="memory_id",
            pk_value="mem_1",
            asset_type="memory",
        )

        self._insert_memory(store, "mem_1", candidate_id, nonce, ciphertext)

        self.assertEqual(
            store.list_memories(),
            [
                {
                    "memory_id": "mem_1",
                    "subtype": "user_preference",
                    "status": "active",
                    "scope": "global",
                    "project_id": None,
                    "source_candidate_id": candidate_id,
                    "source_message_indexes": [],
                    "source_excerpt": "",
                    "source_trace": {
                        "candidate_id": candidate_id,
                        "message_indexes": [],
                        "excerpt": "",
                    },
                    "created_at": 1.0,
                    "updated_at": 2.0,
                    "title": "关闭前审核",
                    "content": "保存可复用记忆",
                }
            ],
        )
        raw_database = Path(workspace.learning_store_path).read_bytes()
        self.assertNotIn("关闭前审核".encode("utf-8"), raw_database)
        self.assertNotIn("保存可复用记忆".encode("utf-8"), raw_database)

    def test_memory_payload_is_bound_to_memory_id(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = store._encrypt_payload(
            {"title": "源记忆", "content": "不能移植到其他行"},
            table="memories",
            pk_name="memory_id",
            pk_value="mem_source",
            asset_type="memory",
        )

        self._insert_memory(store, "mem_source", candidate_id, nonce, ciphertext)
        self._insert_memory(store, "mem_tampered", candidate_id, nonce, ciphertext, updated_at=3.0)

        with self.assertRaises(ValueError):
            store.list_memories()

    def test_memory_payload_reserved_keys_do_not_override_row_fields(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = store._encrypt_payload(
            {
                "memory_id": "forged",
                "status": "archived",
                "source_candidate_id": "cand_forged",
                "title": "真实标题",
            },
            table="memories",
            pk_name="memory_id",
            pk_value="mem_canonical",
            asset_type="memory",
        )

        self._insert_memory(store, "mem_canonical", candidate_id, nonce, ciphertext)

        [memory] = store.list_memories()
        self.assertEqual(memory["memory_id"], "mem_canonical")
        self.assertEqual(memory["status"], "active")
        self.assertEqual(memory["source_candidate_id"], candidate_id)
        self.assertEqual(memory["title"], "真实标题")

    def test_skill_payload_reserved_keys_do_not_override_row_fields(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="skill")
        nonce, ciphertext = store._encrypt_payload(
            {
                "skill_id": "forged",
                "category": "forged-category",
                "status": "archived",
                "version": 99,
                "source_candidate_id": "cand_forged",
                "title": "真实技能",
            },
            table="skills",
            pk_name="skill_id",
            pk_value="skill_canonical",
            asset_type="skill",
        )

        self._insert_skill(store, "skill_canonical", candidate_id, nonce, ciphertext)

        [skill] = store.list_skills()
        self.assertEqual(skill["skill_id"], "skill_canonical")
        self.assertEqual(skill["category"], "workflow")
        self.assertEqual(skill["status"], "active")
        self.assertEqual(skill["version"], 1)
        self.assertEqual(skill["source_candidate_id"], candidate_id)
        self.assertEqual(skill["title"], "真实技能")

    def test_schema_rejects_orphan_candidate_and_invalid_values(self):
        _workspace, store = self._store()

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_candidate(store, review_id="missing_review")

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_review(store, review_id="rev_negative", candidate_count=-1)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_review(store, review_id="rev_negative_accepted", accepted_count=-1)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_review(store, review_id="rev_negative_rejected", rejected_count=-1)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_review(
                store,
                review_id="rev_overcount",
                candidate_count=1,
                accepted_count=1,
                rejected_count=1,
            )

        review_id = self._insert_review(store)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_candidate(store, review_id=review_id, confidence=1.5)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_candidate(store, review_id=review_id, status="queued")

        candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = store._encrypt_payload(
            {"title": "状态约束", "content": "非法状态不应写入"},
            table="memories",
            pk_name="memory_id",
            pk_value="mem_bad_status",
            asset_type="memory",
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_memory(store, "mem_bad_status", candidate_id, nonce, ciphertext, status="pending")

    def test_candidate_payload_is_bound_to_asset_type(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="memory")

        store.conn.execute(
            "UPDATE reflection_candidates SET asset_type = 'skill' WHERE candidate_id = ?",
            (candidate_id,),
        )
        row = store.conn.execute(
            "SELECT * FROM reflection_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()

        with self.assertRaises(ValueError):
            self._candidate_payload_from_row(store, row)

    def test_memory_requires_accepted_memory_candidate(self):
        _workspace, store = self._store()
        skill_candidate_id = self._seed_candidate(store, asset_type="skill")
        nonce, ciphertext = self._memory_payload(store, "mem_from_skill")

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_memory(store, "mem_from_skill", skill_candidate_id, nonce, ciphertext)

        pending_candidate_id = self._seed_candidate(store, asset_type="memory", status="pending")
        nonce, ciphertext = self._memory_payload(store, "mem_from_pending")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_memory(store, "mem_from_pending", pending_candidate_id, nonce, ciphertext)

        rejected_candidate_id = self._seed_candidate(store, asset_type="memory", status="rejected")
        nonce, ciphertext = self._memory_payload(store, "mem_from_rejected")
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_memory(store, "mem_from_rejected", rejected_candidate_id, nonce, ciphertext)

    def test_learning_chunk_requires_active_matching_asset(self):
        _workspace, store = self._store()

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_learning_chunk(store, "chunk_orphan", "missing_memory", "memory")

        memory_candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = self._memory_payload(store, "mem_chunk_source")
        self._insert_memory(store, "mem_chunk_source", memory_candidate_id, nonce, ciphertext)

        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_learning_chunk(store, "chunk_wrong_type", "mem_chunk_source", "skill")

        self._insert_learning_chunk(store, "chunk_memory", "mem_chunk_source", "memory")

    def test_skill_payload_is_bound_to_skill_id(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="skill")
        nonce, ciphertext = store._encrypt_payload(
            {"title": "source skill", "content": "cannot move"},
            table="skills",
            pk_name="skill_id",
            pk_value="skill_source",
            asset_type="skill",
        )

        self._insert_skill(store, "skill_source", candidate_id, nonce, ciphertext)
        self._insert_skill(store, "skill_tampered", candidate_id, nonce, ciphertext, updated_at=3.0)

        with self.assertRaises(ValueError):
            store.list_skills()

    def test_delete_skill_removes_active_skill_chunks(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="skill")
        nonce, ciphertext = store._encrypt_payload(
            {"title": "deletable skill", "content": "delete me"},
            table="skills",
            pk_name="skill_id",
            pk_value="skill_deletable",
            asset_type="skill",
        )

        self._insert_skill(store, "skill_deletable", candidate_id, nonce, ciphertext)
        self._insert_learning_chunk(store, "chunk_skill_deletable", "skill_deletable", "skill")

        self.assertTrue(store.delete_skill("skill_deletable"))
        self.assertEqual(store.list_skills(), [])
        chunk_count = store.conn.execute(
            "SELECT COUNT(*) FROM learning_chunks WHERE asset_type = 'skill' AND asset_id = ?",
            ("skill_deletable",),
        ).fetchone()[0]
        self.assertEqual(chunk_count, 0)

    def test_referenced_memory_candidate_cannot_be_made_unaccepted(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = self._memory_payload(store, "mem_candidate_guard")
        self._insert_memory(store, "mem_candidate_guard", candidate_id, nonce, ciphertext)

        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute(
                "UPDATE reflection_candidates SET status = 'rejected' WHERE candidate_id = ?",
                (candidate_id,),
            )

    def test_referenced_skill_candidate_cannot_change_type_or_status(self):
        _workspace, store = self._store()
        candidate_id = self._seed_candidate(store, asset_type="skill")
        nonce, ciphertext = self._skill_payload(store, "skill_candidate_guard")
        self._insert_skill(store, "skill_candidate_guard", candidate_id, nonce, ciphertext)

        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute(
                "UPDATE reflection_candidates SET asset_type = 'memory' WHERE candidate_id = ?",
                (candidate_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute(
                "UPDATE reflection_candidates SET status = 'rejected' WHERE candidate_id = ?",
                (candidate_id,),
            )

    def test_active_chunks_prevent_asset_archive_or_delete(self):
        _workspace, store = self._store()
        memory_candidate_id = self._seed_candidate(store, asset_type="memory")
        nonce, ciphertext = self._memory_payload(store, "mem_with_chunk")
        self._insert_memory(store, "mem_with_chunk", memory_candidate_id, nonce, ciphertext)
        self._insert_learning_chunk(store, "chunk_for_memory", "mem_with_chunk", "memory")

        skill_candidate_id = self._seed_candidate(store, asset_type="skill")
        nonce, ciphertext = self._skill_payload(store, "skill_with_chunk")
        self._insert_skill(store, "skill_with_chunk", skill_candidate_id, nonce, ciphertext)
        self._insert_learning_chunk(store, "chunk_for_skill", "skill_with_chunk", "skill")

        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE memories SET status = 'archived' WHERE memory_id = 'mem_with_chunk'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("DELETE FROM memories WHERE memory_id = 'mem_with_chunk'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("UPDATE skills SET status = 'archived' WHERE skill_id = 'skill_with_chunk'")
        with self.assertRaises(sqlite3.IntegrityError):
            store.conn.execute("DELETE FROM skills WHERE skill_id = 'skill_with_chunk'")

    def test_candidate_changes_maintain_review_counts(self):
        _workspace, store = self._store()
        review_id = self._insert_review(store, review_id="rev_counts")

        self._insert_candidate(store, candidate_id="cand_pending", review_id=review_id, status="pending")
        self.assertEqual(self._review_counts(store, review_id), (1, 0, 0))

        self._insert_candidate(store, candidate_id="cand_accepted", review_id=review_id, status="accepted")
        self.assertEqual(self._review_counts(store, review_id), (2, 1, 0))

        self._insert_candidate(store, candidate_id="cand_rejected", review_id=review_id, status="rejected")
        self.assertEqual(self._review_counts(store, review_id), (3, 1, 1))

        store.conn.execute("UPDATE reflection_candidates SET status = 'accepted' WHERE candidate_id = 'cand_pending'")
        self.assertEqual(self._review_counts(store, review_id), (3, 2, 1))

        store.conn.execute("UPDATE reflection_candidates SET status = 'rejected' WHERE candidate_id = 'cand_accepted'")
        self.assertEqual(self._review_counts(store, review_id), (3, 1, 2))

        store.conn.execute("DELETE FROM reflection_candidates WHERE candidate_id = 'cand_rejected'")
        self.assertEqual(self._review_counts(store, review_id), (2, 1, 1))

    def test_create_review_persists_pending_candidates(self):
        _workspace, store = self._store()

        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "memory",
                    "subtype": "user_preference",
                    "title": "关闭前集中审核记忆",
                    "content": "用户希望在关闭程序前集中审核候选记忆。",
                    "evidence": ["用户明确说明不要每轮弹窗。"],
                    "rationale": "这是长期交互偏好。",
                    "confidence": 0.91,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": ["preference"],
                },
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "TDD 流程",
                    "content": "先写失败测试，再写最小实现。",
                    "evidence": ["用户要求先写测试。"],
                    "rationale": "可复用流程。",
                    "confidence": 0.83,
                    "selected_by_default": False,
                    "source_message_indexes": [1],
                    "tags": ["workflow"],
                },
            ],
        )

        self.assertEqual(review["status"], "pending")
        self.assertEqual(len(review["candidates"]), 2)
        self.assertEqual(self._review_counts(store, review["review_id"]), (2, 0, 0))
        pending = store.list_pending_reviews()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["candidate_count"], 2)
        self.assertEqual(pending[0]["accepted_count"], 0)
        self.assertEqual(pending[0]["rejected_count"], 0)

    def test_accepting_memory_candidate_creates_memory_and_chunk(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "memory",
                    "subtype": "user_preference",
                    "title": "关闭前集中审核记忆",
                    "content": "用户希望在关闭程序前集中审核候选记忆。",
                    "evidence": ["用户明确说明不要每轮弹窗。"],
                    "rationale": "这是长期交互偏好。",
                    "confidence": 0.91,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": ["preference"],
                }
            ],
        )
        candidate_id = review["candidates"][0]["candidate_id"]

        result = store.decide_review(
            review["review_id"],
            [
                {
                    "candidate_id": candidate_id,
                    "action": "accept",
                    "edited_title": "关闭前集中审核记忆",
                    "edited_content": "关闭前集中审核候选记忆。",
                }
            ],
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.list_pending_reviews(), [])
        self.assertEqual(self._review_counts(store, review["review_id"]), (1, 1, 0))
        memories = store.list_memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["title"], "关闭前集中审核记忆")
        self.assertEqual(memories[0]["content"], "关闭前集中审核候选记忆。")
        chunk = store.conn.execute(
            "SELECT asset_id, asset_type, subtype, status FROM learning_chunks WHERE asset_id = ?",
            (memories[0]["memory_id"],),
        ).fetchone()
        self.assertEqual(chunk["asset_type"], "memory")
        self.assertEqual(chunk["subtype"], "user_preference")
        self.assertEqual(chunk["status"], "active")

    def test_memory_edit_updates_encrypted_search_chunk_and_usage_metadata(self):
        _workspace, store = self._store()
        review = store.create_review("manual", [{
            "asset_type": "memory",
            "subtype": "user_preference",
            "title": "old_pref_token",
            "content": "old_content_token 只用于本次编辑回归。",
            "confidence": 0.9,
            "selected_by_default": True,
            "source_message_indexes": [3],
            "source_excerpt": "用户明确要求旧内容。",
        }])
        accepted = store.decide_review(review["review_id"], [{
            "candidate_id": review["candidates"][0]["candidate_id"],
            "action": "accept",
        }])
        memory_id = accepted["accepted_assets"][0]["memory_id"]
        before = store.list_memories(include_metadata=True)[0]

        updated = store.update_memory(
            memory_id,
            title="new_pref_token",
            content="new_content_token 只用于本次编辑回归。",
            expected_updated_at=before["updated_at"],
        )
        self.assertEqual(updated["title"], "new_pref_token")
        self.assertEqual(updated["content"], "new_content_token 只用于本次编辑回归。")
        self.assertEqual(updated["record_version"], 2)
        self.assertEqual(updated["source_trace"]["message_indexes"], [3])
        self.assertIsNone(updated["last_used_at"])

        old_result = store.search("old_pref_token old_content_token", top_k_memory=3)
        new_result = store.search("new_pref_token new_content_token", top_k_memory=3)
        self.assertEqual(old_result["memories"], [])
        self.assertEqual(new_result["memories"][0]["title"], "new_pref_token")

        self.assertEqual(store.touch_memories([memory_id], used_at=123.0), 1)
        [used] = store.list_memories(include_metadata=True)
        self.assertEqual(used["last_used_at"], 123.0)

    def test_memory_delete_removes_active_record_and_search_chunk(self):
        _workspace, store = self._store()
        review = store.create_review("manual", [{
            "asset_type": "memory",
            "subtype": "user_preference",
            "title": "待删除偏好",
            "content": "这条偏好应从活动索引消失。",
            "confidence": 0.9,
            "selected_by_default": True,
            "source_message_indexes": [],
        }])
        result = store.decide_review(review["review_id"], [{
            "candidate_id": review["candidates"][0]["candidate_id"],
            "action": "accept",
        }])
        memory_id = result["accepted_assets"][0]["memory_id"]

        deleted = store.delete_memory(memory_id)
        self.assertEqual(deleted["status"], "deleted")
        self.assertEqual(store.list_memories(), [])
        self.assertEqual(
            store.conn.execute(
                "SELECT COUNT(*) FROM learning_chunks WHERE asset_id = ?",
                (memory_id,),
            ).fetchone()[0],
            0,
        )
        self.assertIsNone(store.delete_memory(memory_id))

    def test_retrieval_feedback_is_encrypted_scoped_and_exactly_reusable(self):
        workspace, store = self._store()
        global_feedback = store.save_retrieval_feedback(
            "RAG 的核心思想是什么？",
            "RAG 的核心思想是什么？ 检索增强生成 retrieval augmented generation",
        )
        project_feedback = store.save_retrieval_feedback(
            "RAG 的核心思想是什么？",
            "RAG 的核心思想是什么？ 外部知识库 证据引用",
            scope="project",
            project_id="project-a",
        )

        self.assertEqual(
            [item["feedback_id"] for item in store.list_retrieval_feedback(
                "RAG 的核心思想是什么？", project_id="project-a"
            )],
            [project_feedback["feedback_id"], global_feedback["feedback_id"]],
        )
        self.assertEqual(
            store.list_retrieval_feedback("RAG 的核心思想是什么？", project_id="project-b")[0]["feedback_id"],
            global_feedback["feedback_id"],
        )
        self.assertEqual(store.list_retrieval_feedback("完全不同的问题"), [])

        raw_database = Path(workspace.learning_store_path).read_bytes()
        self.assertNotIn("检索增强生成".encode("utf-8"), raw_database)
        self.assertNotIn("外部知识库".encode("utf-8"), raw_database)

    def test_retrieval_feedback_update_replaces_previous_variant_and_revoke_is_fail_closed(self):
        _workspace, store = self._store()
        first = store.save_retrieval_feedback("问题 A", "问题 A 改写一")
        second = store.save_retrieval_feedback("问题 A", "问题 A 改写二")

        self.assertEqual(first["feedback_id"], second["feedback_id"])
        [stored] = store.list_retrieval_feedback("问题 A")
        self.assertEqual(stored["corrected_query"], "问题 A 改写二")

        revoked = store.revoke_retrieval_feedback(first["feedback_id"])
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(store.list_retrieval_feedback("问题 A"), [])
        self.assertIsNone(store.revoke_retrieval_feedback(first["feedback_id"]))

    def test_retrieval_feedback_rejects_blank_same_query_and_missing_project_scope(self):
        _workspace, store = self._store()
        for query, corrected in (("", "改写"), ("问题", ""), ("问题", "问题")):
            with self.assertRaises(ValueError):
                store.save_retrieval_feedback(query, corrected)
        with self.assertRaises(ValueError):
            store.save_retrieval_feedback("问题", "问题 改写", scope="project")

    def test_memory_metadata_marks_runtime_signal_as_unconfirmed(self):
        _workspace, store = self._store()
        review = store.create_review("runtime", [{
            "asset_type": "memory",
            "subtype": "user_preference",
            "title": "运行时偏好",
            "content": "这是显式信号但尚未人工审核。",
            "confidence": 0.9,
            "selected_by_default": True,
            "source_message_indexes": [0],
        }])
        store.decide_review(review["review_id"], [{
            "candidate_id": review["candidates"][0]["candidate_id"],
            "action": "accept",
        }])
        [memory] = store.list_memories(include_metadata=True)
        self.assertEqual(memory["confirmation_status"], "explicit_signal")
        self.assertIsNone(memory["confirmed_at"])

    def test_project_memory_is_visible_with_global_memory_but_not_in_other_project(self):
        _workspace, store = self._store()
        global_review = store.create_review("manual", [{
            "asset_type": "memory", "subtype": "user_preference", "title": "全局偏好",
            "content": "回答使用中文。", "confidence": 0.9,
            "selected_by_default": True, "source_message_indexes": [],
        }])
        store.decide_review(global_review["review_id"], [{
            "candidate_id": global_review["candidates"][0]["candidate_id"], "action": "accept",
        }], project_id="project-a")

        project_review = store.create_review("manual", [{
            "asset_type": "memory", "subtype": "project_fact", "title": "项目事实",
            "content": "项目 A 使用本地模型。", "confidence": 0.9,
            "selected_by_default": True, "source_message_indexes": [],
        }])
        store.decide_review(project_review["review_id"], [{
            "candidate_id": project_review["candidates"][0]["candidate_id"], "action": "accept",
        }], project_id="project-a")

        other_project = store.list_memories(project_id="project-b")
        current_project = store.list_memories(project_id="project-a")
        self.assertEqual({item["title"] for item in other_project}, {"全局偏好"})
        self.assertEqual({item["title"] for item in current_project}, {"全局偏好", "项目事实"})
        self.assertEqual(
            {item["scope"] for item in current_project},
            {"global", "project"},
        )

    def test_context_memory_search_is_scoped_before_top_k_ranking(self):
        _workspace, store = self._store()
        for project_id, title in (("project-a", "项目 A 事实"), ("project-b", "项目 B 事实")):
            review = store.create_review("manual", [{
                "asset_type": "memory", "subtype": "project_fact", "title": title,
                "content": "项目使用本地模型进行检索。", "confidence": 0.9,
                "selected_by_default": True, "source_message_indexes": [],
            }])
            store.decide_review(review["review_id"], [{
                "candidate_id": review["candidates"][0]["candidate_id"], "action": "accept",
            }], project_id=project_id)

        scoped = store.search(
            "项目使用本地模型检索",
            top_k_memory=1,
            project_id="project-a",
            context_only=True,
        )
        global_only = store.list_memories(context_only=True)

        self.assertEqual(scoped["memories"][0]["title"], "项目 A 事实")
        self.assertEqual(global_only, [])

    def test_context_memory_search_without_project_is_global_only_and_keeps_source_trace(self):
        _workspace, store = self._store()
        for project_id, subtype, title in (
            (None, "user_preference", "全局偏好"),
            ("project-a", "project_fact", "项目 A 事实"),
        ):
            review = store.create_review("manual", [{
                "asset_type": "memory", "subtype": subtype, "title": title,
                "content": "回答使用中文并优先引用原始来源。", "confidence": 0.9,
                "selected_by_default": True, "source_message_indexes": [2],
                "source_excerpt": title,
            }])
            store.decide_review(review["review_id"], [{
                "candidate_id": review["candidates"][0]["candidate_id"], "action": "accept",
            }], project_id=project_id)

        global_only = store.search(
            "回答使用中文原始来源",
            top_k_memory=5,
            context_only=True,
        )
        scoped = store.search(
            "回答使用中文原始来源",
            top_k_memory=5,
            project_id="project-a",
            context_only=True,
        )

        self.assertEqual([item["title"] for item in global_only["memories"]], ["全局偏好"])
        self.assertEqual(
            {item["title"] for item in scoped["memories"]},
            {"全局偏好", "项目 A 事实"},
        )
        project_memory = next(item for item in scoped["memories"] if item["title"] == "项目 A 事实")
        self.assertEqual(project_memory["scope"], "project")
        self.assertEqual(project_memory["project_id"], "project-a")
        self.assertEqual(project_memory["source_trace"]["excerpt"], "项目 A 事实")

    def test_rejecting_skill_candidate_does_not_create_skill(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "TDD 流程",
                    "content": "先写失败测试，再写最小实现。",
                    "evidence": ["用户要求先写测试。"],
                    "rationale": "可复用流程。",
                    "confidence": 0.83,
                    "selected_by_default": False,
                    "source_message_indexes": [1],
                    "tags": ["workflow"],
                }
            ],
        )
        candidate_id = review["candidates"][0]["candidate_id"]

        result = store.decide_review(
            review["review_id"],
            [{"candidate_id": candidate_id, "action": "reject"}],
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(store.list_skills(), [])
        self.assertEqual(self._review_counts(store, review["review_id"]), (1, 0, 1))

    def test_accepting_skill_candidate_creates_structured_skill(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "TDD workflow",
                    "content": "1. Write a failing test\n2. Implement minimal code\n3. Run verification",
                    "evidence": ["Use tests first when implementing features."],
                    "rationale": "Reusable implementation workflow.",
                    "confidence": 0.86,
                    "selected_by_default": True,
                    "source_message_indexes": [1],
                    "tags": ["tdd"],
                }
            ],
        )
        candidate_id = review["candidates"][0]["candidate_id"]

        store.decide_review(review["review_id"], [{"candidate_id": candidate_id, "action": "accept"}])

        [skill] = store.list_skills()
        self.assertEqual(skill["title"], "TDD workflow")
        self.assertEqual(skill["intent"], "TDD workflow")
        self.assertGreaterEqual(len(skill["steps"]), 3)
        self.assertEqual(skill["tool_permissions"], [])
        self.assertTrue(skill["output_contract"])

        search = store.search("implement feature tests first", top_k_memory=0, top_k_skill=2)
        self.assertEqual(search["skills"][0]["title"], "TDD workflow")
        self.assertTrue(search["skills"][0]["steps"])

    def test_record_skill_run_persists_encrypted_guided_plan(self):
        workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "TDD workflow",
                    "content": "Write a failing test. Implement minimal code. Run verification.",
                    "evidence": ["Use tests first when implementing features."],
                    "rationale": "Reusable implementation workflow.",
                    "confidence": 0.86,
                    "selected_by_default": True,
                    "source_message_indexes": [1],
                    "tags": ["tdd"],
                }
            ],
        )
        candidate_id = review["candidates"][0]["candidate_id"]
        store.decide_review(review["review_id"], [{"candidate_id": candidate_id, "action": "accept"}])
        [skill] = store.list_skills()

        run = store.record_skill_run(
            skill["skill_id"],
            query="Implement learning validator",
            plan={"steps": [{"index": 1, "instruction": "Write a failing test"}]},
            matched_score=0.82,
        )

        self.assertEqual(run["status"], "planned")
        [stored_run] = store.list_skill_runs()
        self.assertEqual(stored_run["run_id"], run["run_id"])
        self.assertEqual(stored_run["skill_id"], skill["skill_id"])
        self.assertEqual(stored_run["query"], "Implement learning validator")
        raw_database = Path(workspace.learning_store_path).read_bytes()
        self.assertNotIn(b"Implement learning validator", raw_database)

        completed = store.update_skill_run(
            run["run_id"],
            status="completed",
            outcome='{"response_emitted": true}',
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["outcome"], '{"response_emitted": true}')
        self.assertEqual(store.list_skill_runs()[0]["status"], "completed")
        self.assertNotIn(b"response_emitted", raw_database)

    def test_search_recalls_memory_and_skill(self):
        _workspace, store = self._store()
        review = store.create_review(
            trigger="manual",
            candidates=[
                {
                    "asset_type": "memory",
                    "subtype": "user_preference",
                    "title": "关闭前集中审核记忆",
                    "content": "用户希望在关闭程序前集中审核候选记忆。",
                    "evidence": ["用户明确说明不要每轮弹窗。"],
                    "rationale": "这是长期交互偏好。",
                    "confidence": 0.91,
                    "selected_by_default": True,
                    "source_message_indexes": [0],
                    "tags": ["preference"],
                },
                {
                    "asset_type": "skill",
                    "subtype": "sop",
                    "title": "TDD 流程",
                    "content": "先写失败测试，再写最小实现。",
                    "evidence": ["用户要求先写测试。"],
                    "rationale": "可复用流程。",
                    "confidence": 0.83,
                    "selected_by_default": False,
                    "source_message_indexes": [1],
                    "tags": ["workflow"],
                },
            ],
        )
        decisions = [
            {"candidate_id": review["candidates"][0]["candidate_id"], "action": "accept"},
            {"candidate_id": review["candidates"][1]["candidate_id"], "action": "accept"},
        ]
        store.decide_review(review["review_id"], decisions)

        result = store.search("关闭前应该怎么做测试", top_k_memory=3, top_k_skill=3)
        self.assertEqual(len(result["memories"]), 1)
        self.assertEqual(len(result["skills"]), 1)
        self.assertEqual(result["memories"][0]["title"], "关闭前集中审核记忆")
        self.assertEqual(result["memories"][0]["subtype"], "user_preference")
        self.assertEqual(result["skills"][0]["title"], "TDD 流程")
        self.assertEqual(result["skills"][0]["subtype"], "sop")

    def _seed_candidate(self, store, *, asset_type: str, status="accepted"):
        review_id = self._insert_review(store, review_id=f"rev_{asset_type}_{status}")
        candidate_id = f"cand_{asset_type}_{status}"
        self._insert_candidate(
            store,
            candidate_id=candidate_id,
            review_id=review_id,
            asset_type=asset_type,
            status=status,
        )
        return candidate_id

    def _insert_review(
        self,
        store,
        *,
        review_id="rev_1",
        status="pending",
        candidate_count=0,
        accepted_count=0,
        rejected_count=0,
    ):
        store.conn.execute(
            """
            INSERT INTO reflection_reviews (
                review_id, trigger, status, created_at, updated_at,
                candidate_count, accepted_count, rejected_count
            )
            VALUES (?, 'manual', ?, 1.0, 2.0, ?, ?, ?)
            """,
            (review_id, status, candidate_count, accepted_count, rejected_count),
        )
        store.conn.commit()
        return review_id

    def _insert_candidate(
        self,
        store,
        *,
        candidate_id="cand_1",
        review_id="rev_1",
        asset_type="memory",
        status="accepted",
        confidence=0.75,
        selected_by_default=1,
    ):
        nonce, ciphertext = store._encrypt_payload(
            {"title": "候选", "content": "候选内容"},
            table="reflection_candidates",
            pk_name="candidate_id",
            pk_value=candidate_id,
            asset_type=asset_type,
        )
        store.conn.execute(
            """
            INSERT INTO reflection_candidates (
                candidate_id, review_id, asset_type, subtype, status,
                confidence, selected_by_default, duplicate_of,
                source_message_indexes, created_at, updated_at,
                payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, 'user_preference', ?, ?, ?, NULL, '[]', 1.0, 2.0, ?, ?)
            """,
            (candidate_id, review_id, asset_type, status, confidence, selected_by_default, nonce, ciphertext),
        )
        store.conn.commit()
        return candidate_id

    def _insert_memory(
        self,
        store,
        memory_id,
        candidate_id,
        nonce,
        ciphertext,
        *,
        status="active",
        updated_at=2.0,
    ):
        store.conn.execute(
            """
            INSERT INTO memories (
                memory_id, subtype, status, source_candidate_id,
                created_at, updated_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, 'user_preference', ?, ?, 1.0, ?, ?, ?)
            """,
            (memory_id, status, candidate_id, updated_at, nonce, ciphertext),
        )
        store.conn.commit()

    def _insert_skill(self, store, skill_id, candidate_id, nonce, ciphertext, *, updated_at=2.0):
        store.conn.execute(
            """
            INSERT INTO skills (
                skill_id, category, status, version, source_candidate_id,
                created_at, updated_at, payload_nonce, payload_ciphertext
            )
            VALUES (?, 'workflow', 'active', 1, ?, 1.0, ?, ?, ?)
            """,
            (skill_id, candidate_id, updated_at, nonce, ciphertext),
        )
        store.conn.commit()

    def _candidate_payload_from_row(self, store, row):
        return store._decrypt_payload(
            row["payload_nonce"],
            row["payload_ciphertext"],
            table="reflection_candidates",
            pk_name="candidate_id",
            pk_value=row["candidate_id"],
            asset_type=row["asset_type"],
        )

    def _memory_payload(self, store, memory_id):
        return store._encrypt_payload(
            {"title": memory_id, "content": "memory content"},
            table="memories",
            pk_name="memory_id",
            pk_value=memory_id,
            asset_type="memory",
        )

    def _skill_payload(self, store, skill_id):
        return store._encrypt_payload(
            {"title": skill_id, "content": "skill content"},
            table="skills",
            pk_name="skill_id",
            pk_value=skill_id,
            asset_type="skill",
        )

    def _insert_learning_chunk(self, store, chunk_id, asset_id, asset_type):
        nonce, ciphertext = store._encrypt_payload(
            {"text": "learning chunk"},
            table="learning_chunks",
            pk_name="chunk_id",
            pk_value=chunk_id,
            asset_type=asset_type,
        )
        store.conn.execute(
            """
            INSERT INTO learning_chunks (
                chunk_id, asset_id, asset_type, subtype, status,
                text_digest, embedding_json, created_at, updated_at,
                payload_nonce, payload_ciphertext
            )
            VALUES (?, ?, ?, 'user_preference', 'active', 'digest', NULL, 1.0, 2.0, ?, ?)
            """,
            (chunk_id, asset_id, asset_type, nonce, ciphertext),
        )
        store.conn.commit()

    def _review_counts(self, store, review_id):
        row = store.conn.execute(
            """
            SELECT candidate_count, accepted_count, rejected_count
            FROM reflection_reviews
            WHERE review_id = ?
            """,
            (review_id,),
        ).fetchone()
        return (row["candidate_count"], row["accepted_count"], row["rejected_count"])


if __name__ == "__main__":
    unittest.main()
