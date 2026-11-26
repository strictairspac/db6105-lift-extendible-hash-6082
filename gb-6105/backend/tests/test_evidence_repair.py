"""证据缺行与修复恢复的端到端测试，使用 SQLite 内存库，不依赖 Postgres/Redis。

覆盖验收点：
1. 桶证据缺行、与原摘要对不上 -> 标待修并暂停该批下载，其他批次照常；
2. 同批留存件 / 原始卷宗重放两路恢复，桶记录和结论验过原摘要才开放；
3. 两路并发时留存件先过即开放下载，卷宗重放仍在跑；
4. 后到的修复件和重试不重复登记，原签核和原摘要照旧。
"""
import os
import unittest
from copy import deepcopy

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DEMO_PASSWORD", "Demo-Password-2026")

from sqlalchemy import create_engine, select  # noqa: E402

import src.config.resources as resources  # noqa: E402
from src.constants.evidence import EvidenceState, RepairSource, RepairStatus  # noqa: E402
from src.constants.job_state import JobState  # noqa: E402
from src.domain.evidence import drop_bucket_rows  # noqa: E402
from src.domain.scenario import FIXTURES, run as run_scenario  # noqa: E402
from src.models.evidence import evidence_repairs, evidence_snapshots  # noqa: E402
from src.models.reviews import reviews  # noqa: E402
from src.models.runs import runs  # noqa: E402
from src.schemas.reviews import ReviewInput  # noqa: E402
from src.services import evidence, reviews as reviews_svc  # noqa: E402
from src.utils.serial import content_digest  # noqa: E402


class EvidenceRepairTest(unittest.TestCase):
    def setUp(self):
        # StaticPool 让多条连接共享同一个内存库。
        from sqlalchemy.pool import StaticPool
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        resources.engine = self.engine
        from migrations.v001_workbench import upgrade as v1
        from migrations.v002_evidence_repair import upgrade as v2
        with self.engine.begin() as db:
            v1(db)
            v2(db)
        self.payload = deepcopy(FIXTURES[0]["payload"])
        # 造两个批次，验证待修只停自己、其他批次照常。
        self.run_a = self._finish_run(self.payload, 2)
        self.run_b = self._finish_run(deepcopy(self.payload), 2)
        self.original_digest = self.run_a["digest"]

    def _finish_run(self, payload, parameter):
        result = run_scenario(payload, parameter)
        digest = content_digest(result)
        with self.engine.begin() as db:
            from src.models.cases import cases
            from time import time
            case_id = db.execute(cases.insert().values(
                code=f"C-{id(payload)}-{parameter}", title="演示资料", description="d",
                payload=payload, created_by="operator",
            )).inserted_primary_key[0]
            run_id = db.execute(runs.insert().values(
                case_id=case_id, status=JobState.DONE, parameter=parameter, actor="operator",
                created_at=int(time()), finished_at=int(time()), result=result, digest=digest,
                evidence_state=EvidenceState.INTACT,
            )).inserted_primary_key[0]
            evidence.save_snapshot(db, run_id, result, digest)
        return self._get_run(run_id)

    def _get_run(self, run_id):
        with self.engine.connect() as db:
            return dict(db.execute(select(runs).where(runs.c.id == run_id)).mappings().first())

    def _gap(self, run_id, count=1):
        with self.engine.begin() as db:
            return evidence.simulate_gap(db, run_id, count, "operator")

    # 1. 缺行 -> 待修、停下载；另一批不受影响
    def test_gap_marks_repairing_and_blocks_only_that_run(self):
        before_rows = len(self.run_a["result"]["rows"])
        gap = self._gap(self.run_a["id"], 1)
        self.assertEqual(gap["evidence_state"], EvidenceState.REPAIRING)
        damaged = self._get_run(self.run_a["id"])
        self.assertEqual(len(damaged["result"]["rows"]), before_rows - 1)
        with self.engine.connect() as db:
            self.assertEqual(evidence.verify_run(db, self.run_a["id"]), EvidenceState.REPAIRING)
            with self.assertRaises(Exception):
                evidence.ensure_downloadable(db, self.run_a["id"])
            # 另一批仍可下载
            self.assertEqual(evidence.verify_run(db, self.run_b["id"]), EvidenceState.INTACT)
            evidence.ensure_downloadable(db, self.run_b["id"])

    # 2a. 留存件恢复：验过原摘要即开放
    def test_retained_copy_restores_download(self):
        self._gap(self.run_a["id"], 2)
        snapshot_result = self._snapshot(self.run_a["id"])
        with self.engine.begin() as db:
            repair = evidence.submit_retained(db, self.run_a["id"], snapshot_result, "operator")
        self.assertEqual(repair["status"], RepairStatus.VERIFIED)
        run = self._get_run(self.run_a["id"])
        self.assertEqual(run["evidence_state"], EvidenceState.REPAIRED)
        self.assertEqual(run["digest"], self.original_digest)
        with self.engine.connect() as db:
            evidence.ensure_downloadable(db, self.run_a["id"])

    # 2b. 留存件与原摘要不符 -> 驳回，不开放
    def test_retained_mismatch_is_rejected(self):
        self._gap(self.run_a["id"], 1)
        bad, _ = drop_bucket_rows(self._snapshot(self.run_a["id"]), 1)
        with self.engine.begin() as db:
            repair = evidence.submit_retained(db, self.run_a["id"], bad, "operator")
        self.assertEqual(repair["status"], RepairStatus.REJECTED)
        with self.engine.connect() as db:
            with self.assertRaises(Exception):
                evidence.ensure_downloadable(db, self.run_a["id"])

    # 2c. 原始卷宗重放：worker 重放并核验后开放
    def test_replay_restores_after_worker(self):
        self._gap(self.run_a["id"], 2)
        with self.engine.begin() as db:
            repair = evidence.request_replay(db, self.run_a["id"], "operator")
        self.assertEqual(repair["status"], RepairStatus.REPLAYING)
        # worker 领取并重放
        with self.engine.begin() as db:
            claimed = evidence.claim_replay(db)
        self.assertIsNotNone(claimed)
        replayed = run_scenario(claimed["case_payload"], claimed["parameter"])
        with self.engine.begin() as db:
            evidence.complete_replay(db, claimed, replayed)
        run = self._get_run(self.run_a["id"])
        self.assertEqual(run["evidence_state"], EvidenceState.REPAIRED)
        self.assertEqual(run["digest"], self.original_digest)

    # 3. 两路并发：留存件先过即开放，卷宗重放随后完成不回滚
    def test_concurrent_retained_opens_while_replay_running(self):
        self._gap(self.run_a["id"], 2)
        with self.engine.begin() as db:
            evidence.request_replay(db, self.run_a["id"], "operator")
        # 重放还在跑（worker 尚未处理），此时留存件先到
        snapshot_result = self._snapshot(self.run_a["id"])
        with self.engine.begin() as db:
            evidence.submit_retained(db, self.run_a["id"], snapshot_result, "operator")
        run = self._get_run(self.run_a["id"])
        self.assertEqual(run["evidence_state"], EvidenceState.REPAIRED)
        with self.engine.connect() as db:
            evidence.ensure_downloadable(db, self.run_a["id"])  # 已开放
        # 重放随后完成，仍登记成功，但不改变已恢复状态与摘要
        with self.engine.begin() as db:
            claimed = evidence.claim_replay(db)
        replayed = run_scenario(claimed["case_payload"], claimed["parameter"])
        with self.engine.begin() as db:
            evidence.complete_replay(db, claimed, replayed)
        run = self._get_run(self.run_a["id"])
        self.assertEqual(run["evidence_state"], EvidenceState.REPAIRED)
        self.assertEqual(run["digest"], self.original_digest)

    # 4. 幂等：重复登记不新增，原签核和摘要照旧
    def test_repairs_are_idempotent_and_review_untouched(self):
        self._gap(self.run_a["id"], 1)
        # 先签核再缺行，验证修复不动签核
        with self.engine.begin() as db:
            review = reviews_svc.sign(db, self.run_a["id"], ReviewInput(note="已核对同意"), "reviewer")
        snapshot_result = self._snapshot(self.run_a["id"])
        with self.engine.begin() as db:
            first = evidence.submit_retained(db, self.run_a["id"], snapshot_result, "operator")
            second = evidence.submit_retained(db, self.run_a["id"], snapshot_result, "operator")
        self.assertEqual(first["id"], second["id"])
        with self.engine.begin() as db:
            replay1 = evidence.request_replay(db, self.run_a["id"], "operator")
            replay2 = evidence.request_replay(db, self.run_a["id"], "operator")
        self.assertEqual(replay1["id"], replay2["id"])
        with self.engine.connect() as db:
            total = db.execute(
                select(evidence_repairs).where(evidence_repairs.c.run_id == self.run_a["id"])
            ).mappings().all()
        self.assertEqual(len(total), 2)  # 每来源仅一条
        run = self._get_run(self.run_a["id"])
        self.assertEqual(run["digest"], self.original_digest)
        with self.engine.connect() as db:
            saved_review = db.execute(
                select(reviews).where(reviews.c.run_id == self.run_a["id"])
            ).mappings().first()
        self.assertEqual(dict(saved_review)["note"], review["note"])
        self.assertEqual(dict(saved_review)["actor"], "reviewer")

    # 已恢复的批次不回退：后到的损坏/重复修复件不改变已开放状态与原摘要
    def test_repaired_run_does_not_regress(self):
        self._gap(self.run_a["id"], 1)
        snapshot_result = self._snapshot(self.run_a["id"])
        with self.engine.begin() as db:
            first = evidence.submit_retained(db, self.run_a["id"], snapshot_result, "operator")
        # 后到的损坏件：重复提交不重复登记，直接回原登记结论
        bad, _ = drop_bucket_rows(snapshot_result, 1)
        with self.engine.begin() as db:
            again = evidence.submit_retained(db, self.run_a["id"], bad, "operator")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["status"], RepairStatus.VERIFIED)
        with self.engine.connect() as db:
            # 已修复批次反复核验都不回退，下载保持开放
            self.assertEqual(evidence.verify_run(db, self.run_a["id"]), EvidenceState.REPAIRED)
            evidence.ensure_downloadable(db, self.run_a["id"])
        run = self._get_run(self.run_a["id"])
        self.assertEqual(run["evidence_state"], EvidenceState.REPAIRED)
        self.assertEqual(run["digest"], self.original_digest)

    def _snapshot(self, run_id):
        with self.engine.connect() as db:
            row = db.execute(
                select(evidence_snapshots).where(evidence_snapshots.c.run_id == run_id)
            ).mappings().first()
        return deepcopy(dict(row)["result"])

    # 纯读判定：即使证据状态列滞后，实时核对也能在只读连接上拦住待修批次
    def test_readonly_verification_blocks_without_persisted_state(self):
        # 直接把证据改坏但不更新 evidence_state 列，模拟列值滞后
        damaged, _ = drop_bucket_rows(self.run_a["result"], 1)
        with self.engine.begin() as db:
            db.execute(runs.update().where(runs.c.id == self.run_a["id"]).values(result=damaged))
        with self.engine.connect() as db:  # 只读连接，无事务提交
            self.assertEqual(evidence.verify_run(db, self.run_a["id"]), EvidenceState.REPAIRING)
            with self.assertRaises(Exception):
                evidence.ensure_downloadable(db, self.run_a["id"])
        # 列值未被只读连接改写
        self.assertEqual(self._get_run(self.run_a["id"])["evidence_state"], EvidenceState.INTACT)
        # 写事务里落库为待修
        with self.engine.begin() as db:
            evidence.mark_repairing(db, self.run_a["id"])
        self.assertEqual(self._get_run(self.run_a["id"])["evidence_state"], EvidenceState.REPAIRING)

    # 重放计算失败：登记失败原因、不开放下载，留存件仍可独立恢复
    def test_replay_failure_is_recorded_and_retained_still_works(self):
        self._gap(self.run_b["id"], 1)
        with self.engine.begin() as db:
            repair = evidence.request_replay(db, self.run_b["id"], "operator")
        with self.engine.begin() as db:
            evidence.fail_replay(db, repair, "卷宗数据缺失")
        with self.engine.connect() as db:
            with self.assertRaises(Exception):
                evidence.ensure_downloadable(db, self.run_b["id"])
        with self.engine.begin() as db:
            saved = evidence.get_repair(db, self.run_b["id"], RepairSource.REPLAY)
        self.assertEqual(saved["status"], RepairStatus.REJECTED)
        self.assertIn("卷宗重放失败", saved["detail"])
        # 留存件这条路不受影响
        snapshot_result = self._snapshot(self.run_b["id"])
        with self.engine.begin() as db:
            retained = evidence.submit_retained(db, self.run_b["id"], snapshot_result, "operator")
        self.assertEqual(retained["status"], RepairStatus.VERIFIED)
        with self.engine.connect() as db:
            evidence.ensure_downloadable(db, self.run_b["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
