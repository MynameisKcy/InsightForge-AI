"""
Long-Term Memory: SQLite 存储对话摘要，按用户 ID 索引。
"""

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

DB_PATH = get_abs_path("database/memory.db")


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


class LongTermMemory:
    """长期记忆：将压缩后的对话摘要持久化到 SQLite。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or DB_PATH
        _ensure_db()
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    turn_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_memory_user
                ON memory_summaries(user_id, created_at DESC)
            """)
            # ── 会话管理表 ──
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON chat_sessions(user_id, updated_at DESC)
            """)
            # 对话历史表（逐轮存储，关联 session_id）
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    turn_index INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)
            # 向后兼容：旧表可能没有 session_id 列（必须在相关索引之前执行）
            try:
                conn.execute("ALTER TABLE conversation_history ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conv_user
                ON conversation_history(user_id, created_at DESC)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_conv_session
                ON conversation_history(session_id, turn_index ASC)
            """)
            conn.commit()

    def save_summary(self, user_id: str, summary: str, turn_count: int = 0):
        """保存对话摘要。"""
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO memory_summaries (user_id, summary, turn_count, created_at) VALUES (?, ?, ?, ?)",
                (user_id, summary, turn_count, datetime.now().isoformat()),
            )
            conn.commit()
        logger.info(f"Saved summary for user {user_id} ({turn_count} turns)")

    def get_recent_summaries(self, user_id: str, limit: int = 5) -> list[dict]:
        """获取用户最近的对话摘要。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT summary, turn_count, created_at FROM memory_summaries "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_user_history(self, user_id: str, limit: int = 50) -> list[dict]:
        """获取用户完整历史记录。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_summaries WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 会话管理 ──

    def create_session(self, user_id: str, title: str = "") -> str:
        """创建新会话，返回 session_id。title 为空时自动用时间戳。"""
        session_id = uuid.uuid4().hex[:12]
        if not title:
            title = f"新会话 {datetime.now().strftime('%m-%d %H:%M')}"
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            conn.execute(
                """INSERT INTO chat_sessions (session_id, user_id, title, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, user_id, title, now, now),
            )
            conn.commit()
        logger.info(f"Created session {session_id} for user {user_id}")
        return session_id

    def update_session_title(self, session_id: str, title: str):
        """更新会话标题（取用户第一条消息的前若干字）。"""
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE session_id = ?",
                (title, now, session_id),
            )
            conn.commit()

    def touch_session(self, session_id: str):
        """更新会话的 updated_at 时间戳。"""
        with self._get_conn() as conn:
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()

    def get_user_sessions(self, user_id: str, limit: int = 50) -> list[dict]:
        """获取用户的所有会话列表，按最近活跃时间降序排列。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT session_id, user_id, title, created_at, updated_at
                   FROM chat_sessions
                   WHERE user_id = ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_session_owner(self, session_id: str) -> str | None:
        """返回指定会话的归属 user_id；会话不存在返回 None。

        用于 /api/sessions/{id} 端点做 IDOR 归属校验，防止用户读取他人会话历史。
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM chat_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row["user_id"] if row else None

    def get_session_conversation(self, session_id: str) -> list[dict]:
        """获取指定会话的完整对话历史。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT id, user_id, session_id, role, content, turn_index, created_at
                   FROM conversation_history
                   WHERE session_id = ?
                   ORDER BY turn_index ASC, role ASC""",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ── 对话历史（逐轮存储） ──

    def save_turn(self, user_id: str, role: str, content: str, turn_index: int = 0):
        """保存单轮对话到长期记忆。"""
        with self._get_conn() as conn:
            conn.execute(
                """INSERT INTO conversation_history (user_id, role, content, turn_index, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, role, content, turn_index, datetime.now().isoformat()),
            )
            conn.commit()

    def save_conversation_pair(self, user_id: str, user_msg: str, assistant_msg: str,
                               session_id: str = ""):
        """保存一对问答（用户消息 + 助手回复），自动计算 turn_index。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn_index), -1) AS max_idx FROM conversation_history WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            next_idx = (row["max_idx"] or -1) + 1
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO conversation_history (user_id, session_id, role, content, turn_index, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, session_id, "user", user_msg, next_idx, now),
            )
            conn.execute(
                "INSERT INTO conversation_history (user_id, session_id, role, content, turn_index, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, session_id, "assistant", assistant_msg, next_idx, now),
            )
            conn.commit()
            # 同时更新会话的 updated_at
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()

    def get_user_conversations(self, user_id: str, limit: int = 50) -> list[dict]:
        """获取用户完整对话历史。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                """SELECT id, user_id, role, content, turn_index, created_at
                   FROM conversation_history
                   WHERE user_id = ?
                   ORDER BY created_at ASC, turn_index ASC
                   LIMIT ?""",
                (user_id, limit * 2),  # user + assistant = 2 per turn
            ).fetchall()
            return [dict(r) for r in rows]

    def get_last_n_turns(self, user_id: str, n: int = 10) -> list[dict]:
        """获取用户最近 N 轮对话（按 turn_index 去重后取最近 N 个）。"""
        with self._get_conn() as conn:
            # 先获取最近的 N 个 turn_index
            rows = conn.execute(
                """SELECT DISTINCT turn_index FROM conversation_history
                   WHERE user_id = ?
                   ORDER BY turn_index DESC
                   LIMIT ?""",
                (user_id, n),
            ).fetchall()
            if not rows:
                return []
            indices = [r["turn_index"] for r in rows]
            placeholders = ",".join("?" * len(indices))
            rows = conn.execute(
                f"""SELECT role, content, turn_index, created_at FROM conversation_history
                    WHERE user_id = ? AND turn_index IN ({placeholders})
                    ORDER BY turn_index ASC, role ASC""",
                (user_id, *indices),
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_user_data(self, user_id: str):
        """删除用户的所有记忆数据。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM memory_summaries WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM conversation_history WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM chat_sessions WHERE user_id = ?", (user_id,))
            conn.commit()
