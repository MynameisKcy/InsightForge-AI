import database.user_settings_db as usd_mod


def _fresh_db(tmp_path):
    """给模块换一个临时 DB_PATH，避免污染真实库。"""
    usd_mod.DB_PATH = str(tmp_path / "user_settings.db")
    usd_mod._ensure_db()
    usd_mod._init_db()
    return usd_mod.UserSettingsDB()

def test_upsert_get_roundtrip(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_api_key": "sk-caa0410b364d47f09774d0c3b2b64213",
                     "llm_model_name": "qwen-max",
                     "embedding_model_name": "text-embedding-v2",
                     "local_db_conn": "sqlite:///db.db"})
    got = db.get("u1")
    assert got["llm_api_key"] == "sk-caa0410b364d47f09774d0c3b2b64213"
    assert got["llm_model_name"] == "qwen-max"

def test_get_none_when_absent(tmp_path):
    db = _fresh_db(tmp_path)
    assert db.get("nope") is None
    assert db.has("nope") is False

def test_masked_hides_full_key(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_api_key": "sk-caa0410b364d47f09774d0c3b2b64213",
                     "llm_model_name": "qwen-max"})
    masked = db.get_masked("u1")
    assert masked["llm_api_key"].startswith("sk-")
    assert masked["llm_api_key"].endswith("4213")  # 末尾4位可见
    assert "caa0410b364d47f09774d0c3" not in masked["llm_api_key"]  # 中间不可见

def test_storage_is_encrypted(tmp_path):
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_api_key": "sk-secretkey123456",
                     "llm_model_name": "qwen-max"})
    # 直接读 SQLite 文件，明文不应出现
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "user_settings.db"))
    row = conn.execute("SELECT llm_api_key_enc FROM user_settings WHERE user_id=?", ("u1",)).fetchone()
    conn.close()
    assert "sk-secretkey123456" not in row[0]


def test_enable_thinking_roundtrip_and_preserve(tmp_path):
    """思考开关：未设置 -> None（回落 env/YAML）；显式保存 -> bool；None 不清掉已存值。"""
    db = _fresh_db(tmp_path)
    db.upsert("u1", {"llm_model_name": "qwen-max"})
    assert db.get("u1")["llm_enable_thinking"] is None          # 未设置回落默认层
    db.upsert("u1", {"llm_model_name": "qwen-max", "llm_enable_thinking": True})
    assert db.get("u1")["llm_enable_thinking"] is True
    # 再存一次但不带该键（前端未改动不上传）：COALESCE 保住已存 true，不清回 None
    db.upsert("u1", {"llm_model_name": "qwen2-max"})
    got = db.get("u1")
    assert got["llm_enable_thinking"] is True
    assert got["llm_model_name"] == "qwen2-max"
