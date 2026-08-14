"""
用户配置存储：每用户一行，API Key 用 Fernet 对称加密。
优先级（消费侧在 factory.getter 实现）：用户配置 > .env > YAML 默认。
"""
import os
import sqlite3

from utils.logger_handler import logger
from utils.path_tool import get_abs_path

DB_PATH = get_abs_path("database/user_settings.db")


def _ensure_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _get_fernet():
    """获取 Fernet：密钥来自环境变量 INSIGHTFORGE_SETTINGS_KEY；缺失则生成并写 .env。

    显式加载 .env，避免每次进程启动时因环境变量未注入而误生成新密钥
    （新密钥会使此前加密的 API Key 无法解密）。
    """
    from cryptography.fernet import Fernet
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        load_dotenv(env_path, override=False)
    except ImportError:
        pass
    key = os.environ.get("INSIGHTFORGE_SETTINGS_KEY")
    if not key:
        key = Fernet.generate_key().decode()
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        try:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"INSIGHTFORGE_SETTINGS_KEY={key}\n")
        except OSError as e:
            logger.warning(f"写入 INSIGHTFORGE_SETTINGS_KEY 到 .env 失败: {e}")
        os.environ["INSIGHTFORGE_SETTINGS_KEY"] = key
        logger.info("已生成并写入 INSIGHTFORGE_SETTINGS_KEY")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _init_db():
    _ensure_db()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        llm_api_key_enc TEXT,
        llm_model_name TEXT,
        embedding_model_name TEXT,
        vector_db_host TEXT,
        vector_db_port TEXT,
        vector_db_collection TEXT,
        vector_db_tenant TEXT,
        local_db_conn TEXT,
        updated_at TEXT
    )""")
    # 幂等迁移：补齐 llm_base_url 列（旧表无此列）
    try:
        conn.execute("ALTER TABLE user_settings ADD COLUMN llm_base_url TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def mask_api_key(key: str) -> str:
    """掩码：保留前缀与末4位，中间用 ****。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]


class UserSettingsDB:
    def __init__(self):
        _init_db()

    def _connect(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def has(self, user_id: str) -> bool:
        conn = self._connect()
        row = conn.execute("SELECT user_id FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        return row is not None

    def get(self, user_id: str) -> dict | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        if row is None:
            return None
        f = _get_fernet()
        api_key = ""
        if row["llm_api_key_enc"]:
            try:
                api_key = f.decrypt(row["llm_api_key_enc"].encode()).decode()
            except Exception as e:
                logger.warning(f"API Key 解密失败: {e}")
        return {
            "llm_api_key": api_key,
            "llm_model_name": row["llm_model_name"],
            "embedding_model_name": row["embedding_model_name"],
            "llm_base_url": row["llm_base_url"],
            "vector_db_host": row["vector_db_host"],
            "vector_db_port": row["vector_db_port"],
            "vector_db_collection": row["vector_db_collection"],
            "vector_db_tenant": row["vector_db_tenant"],
            "local_db_conn": row["local_db_conn"],
            "updated_at": row["updated_at"],
        }

    def get_masked(self, user_id: str) -> dict | None:
        data = self.get(user_id)
        if data is None:
            return None
        data["llm_api_key"] = mask_api_key(data.get("llm_api_key", ""))
        return data

    def upsert(self, user_id: str, settings: dict) -> None:
        from datetime import datetime
        f = _get_fernet()
        api_key = settings.get("llm_api_key", "")
        enc = f.encrypt(api_key.encode()).decode() if api_key else None
        now = datetime.now().isoformat(timespec="seconds")
        conn = self._connect()
        conn.execute("""INSERT INTO user_settings
            (user_id, llm_api_key_enc, llm_model_name, embedding_model_name,
             llm_base_url, vector_db_host, vector_db_port, vector_db_collection,
             vector_db_tenant, local_db_conn, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              llm_api_key_enc=excluded.llm_api_key_enc,
              llm_model_name=excluded.llm_model_name,
              embedding_model_name=excluded.embedding_model_name,
              llm_base_url=excluded.llm_base_url,
              vector_db_host=excluded.vector_db_host,
              vector_db_port=excluded.vector_db_port,
              vector_db_collection=excluded.vector_db_collection,
              vector_db_tenant=excluded.vector_db_tenant,
              local_db_conn=excluded.local_db_conn,
              updated_at=excluded.updated_at""",
            (user_id, enc, settings.get("llm_model_name"),
             settings.get("embedding_model_name"), settings.get("llm_base_url"),
             settings.get("vector_db_host"),
             settings.get("vector_db_port"), settings.get("vector_db_collection"),
             settings.get("vector_db_tenant"), settings.get("local_db_conn"), now))
        conn.commit()
        conn.close()


user_settings_db = UserSettingsDB()
