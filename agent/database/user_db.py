"""
User Database: SQLite 用户表，存储 id、账号、密码哈希、历史会话。
"""

import hashlib
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta

import bcrypt

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
for path in (PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

DB_PATH = get_abs_path("database/users.db")


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


# 旧版密码哈希（SHA-256 无 salt）用 64 位十六进制，新版 bcrypt 以 $2 开头。
_LEGACY_HASH_LEN = 64


def _is_legacy_hash(pwd_hash: str) -> bool:
    """判断是否为旧版 SHA-256 哈希（需惰性升级）。"""
    return bool(pwd_hash) and not pwd_hash.startswith("$2") and len(pwd_hash) == _LEGACY_HASH_LEN


def _hash_password(password: str) -> str:
    """使用 bcrypt（带随机 salt）哈希密码。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, pwd_hash: str) -> bool:
    """校验密码：兼容新版 bcrypt 与旧版 SHA-256（用于惰性升级）。"""
    if not pwd_hash:
        return False
    if _is_legacy_hash(pwd_hash):
        # 旧版 SHA-256 比对（用恒定时间比较防侧信道）
        calc = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return secrets.compare_digest(calc, pwd_hash)
    try:
        return bcrypt.checkpw(password.encode("utf-8"), pwd_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


class UserDB:
    """用户数据库管理。"""

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
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    account TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_login TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    session_token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_token
                ON sessions(session_token)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_sessions_user
                ON sessions(user_id)
            """)
            conn.commit()

    # ── 用户管理 ──

    def register(self, account: str, password: str, user_id: str | None = None) -> dict:
        """注册新用户。返回用户信息或错误。"""
        if not account or not password:
            return {"success": False, "error": "账号和密码不能为空"}
        if len(password) < 8:
            return {"success": False, "error": "密码长度至少 8 位"}
        if len(account) < 2:
            return {"success": False, "error": "账号长度至少 2 位"}

        user_id = user_id or f"u_{account}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        pwd_hash = _hash_password(password)

        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO users (id, account, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, account, pwd_hash, datetime.now().isoformat()),
                )
                conn.commit()
            logger.info(f"User registered: {account} (id={user_id})")
            return {"success": True, "user_id": user_id, "account": account}
        except sqlite3.IntegrityError as e:
            if "account" in str(e):
                return {"success": False, "error": "账号已存在"}
            return {"success": False, "error": f"注册失败: {e}"}

    def login(self, account: str, password: str) -> dict:
        """用户登录。返回用户信息和 session token。

        兼容旧版 SHA-256 哈希：登录成功时惰性升级为 bcrypt。
        """
        if not account or not password:
            return {"success": False, "error": "账号和密码不能为空"}

        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, account, password_hash FROM users WHERE account = ?",
                (account,),
            ).fetchone()

            # 恒定时间校验，避免账号是否存在侧信道（尽量统一耗时）
            valid = bool(row) and _verify_password(password, row["password_hash"] if row else "")
            if not row or not valid:
                # 仍执行一次无意义比较，缩小时序差
                _verify_password(password, "$2b$12$" + "0" * 53)
                return {"success": False, "error": "账号或密码错误"}

            user_id = row["id"]
            # 惰性升级：旧版 SHA-256 哈希登录成功后重写为 bcrypt
            if _is_legacy_hash(row["password_hash"]):
                new_hash = _hash_password(password)
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (new_hash, user_id),
                )
                logger.info(f"User {account} password hash upgraded to bcrypt")

            # 创建 session
            import uuid
            token = secrets.token_hex(16)
            now = datetime.now()
            expires = now + timedelta(hours=24)  # 24 小时有效

            conn.execute(
                "INSERT INTO sessions (user_id, session_token, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (user_id, token, now.isoformat(), expires.isoformat()),
            )
            conn.execute(
                "UPDATE users SET last_login = ? WHERE id = ?",
                (now.isoformat(), user_id),
            )
            conn.commit()

        logger.info(f"User logged in: {account}")
        return {
            "success": True,
            "user_id": user_id,
            "account": row["account"],
            "token": token,
        }

    def validate_token(self, token: str) -> dict | None:
        """验证 session token，返回用户信息或 None。使用恒定时间比较防侧信道。"""
        if not token:
            return None

        with self._get_conn() as conn:
            row = conn.execute(
                """SELECT u.id, u.account, s.expires_at, s.session_token
                   FROM sessions s JOIN users u ON s.user_id = u.id
                   WHERE s.session_token = ?""",
                (token,),
            ).fetchone()

            if not row:
                return None

            # 恒定时间比较 token（虽然已用 WHERE 精确匹配，仍加此道防时序侧信道）
            if not secrets.compare_digest(str(row["session_token"]), token):
                return None

            if row["expires_at"] < datetime.now().isoformat():
                # Token 过期
                conn.execute("DELETE FROM sessions WHERE session_token = ?", (token,))
                conn.commit()
                return None

            return {"user_id": row["id"], "account": row["account"]}

    def logout(self, token: str):
        """登出，删除 session。"""
        with self._get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE session_token = ?", (token,))
            conn.commit()

    def get_user(self, user_id: str) -> dict | None:
        """获取用户信息。"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, account, created_at, last_login FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_session_history(self, user_id: str, limit: int = 50) -> list[dict]:
        """获取用户历史 session 记录。"""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]


# 全局实例
user_db = UserDB()
