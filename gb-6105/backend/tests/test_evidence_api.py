"""证据修复流程的 HTTP 接口测试：路由、下载门禁、权限与幂等。"""
import os
import unittest
from copy import deepcopy

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("DEMO_PASSWORD", "Demo-Password-2026")
os.environ["SEED_DEMO"] = "false"  # 测试库自己建账号，跳过仅支持 PG/MySQL 的演示 seed

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.resources as resources  # noqa: E402

# 必须在导入 app/服务模块前替换 engine：这些模块在导入时即绑定 engine。
_TEST_ENGINE = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
resources.engine = _TEST_ENGINE

from src.constants.evidence import EvidenceState, RepairStatus  # noqa: E402
from src.constants.job_state import JobState  # noqa: E402
from src.domain.scenario import FIXTURES, run as run_scenario  # noqa: E402
from src.main import app  # noqa: E402
from src.models.evidence import evidence_snapshots  # noqa: E402
from src.models.runs import runs  # noqa: E402
from src.services import evidence  # noqa: E402
from src.utils.passwords import hash_password  # noqa: E402
from src.utils.serial import content_digest  # noqa: E402

PASSWORD = "Demo-Password-2026"


class EvidenceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _TEST_ENGINE
        from migrations.v001_workbench import upgrade as v1
        from migrations.v002_evidence_repair import upgrade as v2
        with cls.engine.begin() as db:
            v1(db)
            v2(db)
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls._ensure_users()
        cls.run_a, cls.run_b = cls._seed_run(), cls._seed_run()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @classmethod
    def _ensure_users(cls):
        from src.models.users import users as user_table
        from src.constants.roles import Role
        with cls.engine.begin() as db:
            existing = set(db.execute(select(user_table.c.username)).scalars())
            for role in Role:
                if role.value not in existing:
                    db.execute(user_table.insert().values(
                        username=role.value, role=role.value,
                        password_hash=hash_password(PASSWORD)))

    @classmethod
    def _seed_run(cls):
        from time import time
        from src.models.cases import cases
        payload = deepcopy(FIXTURES[0]["payload"])
        result = run_scenario(payload, 2)
        digest = content_digest(result)
        with cls.engine.begin() as db:
            case_id = db.execute(cases.insert().values(
                code=f"C-{time()}-{id(payload)}", title="演示资料", description="d",
                payload=payload, created_by="operator")).inserted_primary_key[0]
            run_id = db.execute(runs.insert().values(
                case_id=case_id, status=JobState.DONE, parameter=2, actor="operator",
                created_at=int(time()), finished_at=int(time()), result=result, digest=digest,
                evidence_state=EvidenceState.INTACT)).inserted_primary_key[0]
            evidence.save_snapshot(db, run_id, result, digest)
        return run_id

    def _token(self, username):
        resp = self.client.post("/auth/login", json={"username": username, "password": PASSWORD})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["token"]

    def _auth(self, username):
        return {"Authorization": f"Bearer {self._token(username)}"}

    def test_full_flow_export_gate_and_two_repairs(self):
        operator = self._auth("operator")
        # 初始：完整可下载
        overview = self.client.get(f"/runs/{self.run_a}/evidence", headers=operator)
        self.assertEqual(overview.status_code, 200)
        self.assertTrue(overview.json()["downloadable"])
        self.assertEqual(self.client.get(f"/runs/{self.run_a}/export", headers=operator).status_code, 200)

        # 缺行 -> 待修，导出 409；另一批照常 200
        gapped = self.client.post(f"/runs/{self.run_a}/evidence/gap", headers=operator, json={"count": 2})
        self.assertEqual(gapped.status_code, 200)
        self.assertEqual(gapped.json()["evidence_state"], EvidenceState.REPAIRING)
        self.assertEqual(self.client.get(f"/runs/{self.run_a}/export", headers=operator).status_code, 409)
        self.assertEqual(self.client.get(f"/runs/{self.run_b}/export", headers=operator).status_code, 200)

        # 卷宗重放登记（worker 未在测试进程内，状态保持 replaying）
        replay = self.client.post(f"/runs/{self.run_a}/evidence/repairs/replay", headers=operator)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()["status"], RepairStatus.REPLAYING)
        replay_again = self.client.post(f"/runs/{self.run_a}/evidence/repairs/replay", headers=operator)
        self.assertEqual(replay_again.json()["id"], replay.json()["id"])  # 重试不重复登记

        # 留存件先恢复 -> 立即开放下载
        snapshot = self.client.get(f"/runs/{self.run_a}/evidence/snapshot", headers=operator).json()
        retained = self.client.post(
            f"/runs/{self.run_a}/evidence/repairs/retained", headers=operator,
            json={"result": snapshot["result"]})
        self.assertEqual(retained.status_code, 201)
        self.assertEqual(retained.json()["status"], RepairStatus.VERIFIED)
        self.assertEqual(self.client.get(f"/runs/{self.run_a}/export", headers=operator).status_code, 200)

        # 概览显示已恢复，且修复登记每来源仅一条
        overview = self.client.get(f"/runs/{self.run_a}/evidence", headers=operator).json()
        self.assertEqual(overview["evidence_state"], EvidenceState.REPAIRED)
        self.assertEqual(len(overview["repairs"]), 2)

    def test_retained_mismatch_keeps_download_blocked(self):
        operator = self._auth("operator")
        self.client.post(f"/runs/{self.run_b}/evidence/gap", headers=operator, json={"count": 1})
        snapshot = self.client.get(f"/runs/{self.run_b}/evidence/snapshot", headers=operator).json()
        from src.domain.evidence import drop_bucket_rows
        bad, _ = drop_bucket_rows(snapshot["result"], 1)
        resp = self.client.post(
            f"/runs/{self.run_b}/evidence/repairs/retained", headers=operator, json={"result": bad})
        self.assertEqual(resp.json()["status"], RepairStatus.REJECTED)
        self.assertEqual(self.client.get(f"/runs/{self.run_b}/export", headers=operator).status_code, 409)

    def test_viewer_cannot_trigger_gap_or_repair(self):
        viewer = self._auth("viewer")
        resp = self.client.post(f"/runs/{self.run_a}/evidence/gap", headers=viewer, json={"count": 1})
        self.assertEqual(resp.status_code, 403)
        resp = self.client.post(
            f"/runs/{self.run_a}/evidence/repairs/retained", headers=viewer, json={"result": {}})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_is_rejected(self):
        self.assertEqual(self.client.get(f"/runs/{self.run_a}/evidence").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
