"""/api/tasks 端点（#1 Task System）：列表 / 详情 / resume 接线。

- 存储走 tmp 根（set_tasks_root），不触达生产 data/tasks；
- planner 经 swap_srv_seam("_get_planner_agent") 换桩，序列化走真实现。
"""
import pytest

from memory.task_store import TaskRecord, new_task_id, save_task, set_tasks_root


@pytest.fixture(autouse=True)
def _tmp_tasks_root(tmp_path):
    set_tasks_root(str(tmp_path))
    yield
    set_tasks_root(None)


class _FakeResumer:
    def __init__(self, result=None):
        self.result = result or {"success": True, "report": {"markdown": "# 续跑报告"}}
        self.calls = []

    def resume(self, task_id, user_id, session_id=""):
        self.calls.append((task_id, user_id, session_id))
        return self.result


def _seed_task(owner="u1", **kw):
    base = dict(id=new_task_id(), owner=owner, query="分析趋势",
                title="趋势报告", plan=[{"step": 1, "agent": "sql_query"}],
                status="running")
    base.update(kw)
    return save_task(TaskRecord(**base))


def test_list_tasks_empty(client, auth_headers):
    r = client.get("/api/tasks", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"tasks": []}


def test_list_tasks_returns_summary(client, auth_headers, auth):
    rec = _seed_task(owner=auth["user_id"])
    r = client.get("/api/tasks", headers=auth_headers)
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) == 1
    s = tasks[0]
    assert s["id"] == rec.id
    assert s["title"] == "趋势报告"
    assert s["status"] == "running"
    assert "plan" not in s  # 列表页不返回全量 plan


def test_get_task_detail(client, auth_headers, auth):
    rec = _seed_task(owner=auth["user_id"],
                     plan=[{"step": 1, "agent": "sql_query", "depends_on": []}])
    r = client.get(f"/api/tasks/{rec.id}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["plan"][0]["agent"] == "sql_query"


def test_get_task_missing_returns_404(client, auth_headers):
    r = client.get("/api/tasks/task_ghost", headers=auth_headers)
    assert r.status_code == 404


def test_get_task_cross_owner_returns_404(client, auth_headers, auth):
    rec = _seed_task(owner="some_other_user")
    # auth 用户不是 seed owner → 404（不泄露存在性）
    r = client.get(f"/api/tasks/{rec.id}", headers=auth_headers)
    assert r.status_code == 404


def test_resume_success(client, auth_headers, auth, swap_srv_seam):
    fake = _FakeResumer()
    swap_srv_seam("_get_planner_agent", lambda uid: fake)
    rec = _seed_task(owner=auth["user_id"])
    r = client.post(f"/api/tasks/{rec.id}/resume", json={}, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["report"]["markdown"] == "# 续跑报告"
    assert fake.calls == [(rec.id, fake.calls[0][1], "")]


def test_resume_business_error_returns_body(client, auth_headers, auth, swap_srv_seam):
    fake = _FakeResumer(result={"success": False, "error": "数据集已变化，请重新发起分析"})
    swap_srv_seam("_get_planner_agent", lambda uid: fake)
    rec = _seed_task(owner=auth["user_id"])
    r = client.post(f"/api/tasks/{rec.id}/resume", json={}, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["error"] == "数据集已变化，请重新发起分析"


def test_resume_unauthenticated(client):
    r = client.post("/api/tasks/task_x/resume", json={})
    assert r.status_code in (401, 403)
