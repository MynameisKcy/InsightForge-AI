"""TaskStore：分析任务的 JSON 文件持久化（#1 Task System，P1）。

对标 s10 的 .tasks/*.json：plan 跨会话存活，depends_on 从「临时变量」变成
可恢复执行的真依赖。要点：

- 存储：``data/tasks/<owner>/<task_id>.json``，owner 隔离（每用户一目录），
  写 = tmp + ``os.replace`` 原子替换（进程崩溃不产生半截文件）。
- 路径安全：owner/task_id 只允许 [A-Za-z0-9_-]（拒绝路径穿越），realpath
  必须落在 data/tasks 之下（复用 safety.validate_csv_path 的检查模式）。
- 无模块级可变用户态（owner 全显式传参），符合多用户隔离约定。
- 测试接缝：``set_tasks_root()`` 仅测试用（tmp 目录），生产默认 data/tasks。
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

# owner / task_id 白名单：拒绝路径穿越与非法字符
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_STATUSES = {"running", "completed", "failed", "cancelled"}

_tasks_root: str | None = None  # 测试接缝：set_tasks_root() 覆写


def set_tasks_root(path: str | None) -> None:
    """覆写存储根目录（仅测试用；生产调用会破坏数据目录语义）。"""
    global _tasks_root
    _tasks_root = path


def tasks_root() -> str:
    if _tasks_root is not None:
        return _tasks_root
    root = get_abs_path("data/tasks")
    os.makedirs(root, exist_ok=True)
    return root


class TaskNotFoundError(Exception):
    """任务不存在或不属于该 owner（调用方按 404 语义处理，防存在性泄露）。"""


class TaskPathError(Exception):
    """owner/task_id 含非法字符或路径越界。"""


@dataclass
class TaskRecord:
    """一次分析任务的持久化快照（可恢复执行的完整状态）。"""

    id: str
    owner: str
    query: str
    title: str
    plan: list[dict] = field(default_factory=list)
    completed_steps: list[int] = field(default_factory=list)
    # agent_name → 完整结果 dict（含降级占位）；resume 回灌 PipelineContext
    stage_results: dict = field(default_factory=dict)
    # SQL 步产物快照：下游续跑的数据载体，避免 resume 时重查库
    dataframe_json: str = ""
    # 数据集一致性校验：resume 时 DataResolver 重解析后比对
    dataset_name: str = ""
    primary_table: str = ""
    session_id: str = ""
    status: str = "running"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def _owner_dir(owner: str) -> str:
    if not _SAFE_NAME_RE.match(owner or ""):
        raise TaskPathError(f"非法 owner: {owner!r}")
    return os.path.join(tasks_root(), owner)


def _task_path(owner: str, task_id: str) -> str:
    if not _SAFE_NAME_RE.match(task_id or ""):
        raise TaskPathError(f"非法 task_id: {task_id!r}")
    path = os.path.join(_owner_dir(owner), f"{task_id}.json")
    real_root = os.path.realpath(tasks_root())
    if not os.path.realpath(path).startswith(real_root + os.sep):
        raise TaskPathError(f"任务路径越界: {path!r}")
    return path


def new_task_id() -> str:
    """task_<时间戳>_<同秒单调递增后缀>（列表排序的稳定 tie-break）。"""
    stamp = time.strftime("%Y%m%d%H%M%S")
    micro = int(time.time() * 1_000_000) % 1_000_000
    return f"task_{stamp}_{micro:06d}"


def _now() -> str:
    """毫秒精度 ISO 时间戳（同一秒内多次写可区分，保证列表排序确定性）。"""
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def save_task(record: TaskRecord) -> TaskRecord:
    """原子写（tmp + os.replace）。owner 目录自动创建。"""
    path = _task_path(record.owner, record.id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record.updated_at = _now()
    if not record.created_at:
        record.created_at = record.updated_at
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return record


def get_task(owner: str, task_id: str) -> TaskRecord | None:
    """按 owner 取任务；不存在/越权返回 None（不抛，调用方按 404）。"""
    try:
        path = _task_path(owner, task_id)
    except TaskPathError:
        return None
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        rec = TaskRecord.from_dict(data)
        # 双保险：文件内 owner 字段必须与查询 owner 一致
        return rec if rec.owner == owner else None
    except Exception as e:
        logger.warning(f"task_store.get_task failed ({owner}/{task_id}): {e}")
        return None


def list_tasks(owner: str, limit: int = 20,
               exclude_completed: bool = False) -> list[TaskRecord]:
    """按 owner 列出任务，created_at 降序，最多 limit 条。"""
    try:
        d = _owner_dir(owner)
    except TaskPathError:
        return []
    if not os.path.isdir(d):
        return []
    recs: list[TaskRecord] = []
    for name in os.listdir(d):
        if not name.endswith(".json"):
            continue
        rec = get_task(owner, name[:-5])
        if rec is None:
            continue
        if exclude_completed and rec.status == "completed":
            continue
        recs.append(rec)
    recs.sort(key=lambda r: (r.created_at, r.id), reverse=True)
    return recs[:limit]


def update_progress(owner: str, task_id: str, *, completed_steps: list[int],
                    stage_results: dict | None = None,
                    dataframe_json: str | None = None,
                    status: str | None = None) -> TaskRecord | None:
    """读-改-写：推进 completed_steps / 阶段结果 / 终态。失败返回 None（调用方降级）。"""
    rec = get_task(owner, task_id)
    if rec is None:
        return None
    rec.completed_steps = sorted(set(completed_steps))
    if stage_results:
        rec.stage_results.update(stage_results)
    if dataframe_json is not None:
        rec.dataframe_json = dataframe_json
    if status is not None:
        if status not in _STATUSES:
            raise ValueError(f"非法任务状态: {status!r}")
        rec.status = status
    try:
        return save_task(rec)
    except Exception as e:
        logger.warning(f"task_store.update_progress failed ({owner}/{task_id}): {e}")
        return None
