import database.user_settings_db as usd_mod


def _fresh(tmp_path):
    usd_mod.DB_PATH = str(tmp_path / "u.db")
    usd_mod._ensure_db()
    usd_mod._init_db()
    return usd_mod.UserSettingsDB()

def test_user_a_invisible_to_b(tmp_path):
    db = _fresh(tmp_path)
    db.upsert("A", {"llm_api_key": "sk-aaa", "llm_model_name": "qwen-a"})
    assert db.get("B") is None
    assert db.has("B") is False

def test_masked_does_not_leak_full_key_across_users(tmp_path):
    db = _fresh(tmp_path)
    db.upsert("A", {"llm_api_key": "sk-secretaaa123456", "llm_model_name": "qwen-a"})
    # B 查不到 A 的配置（含掩码版）
    assert db.get_masked("B") is None

def test_upsert_per_user_isolation(tmp_path):
    db = _fresh(tmp_path)
    db.upsert("A", {"llm_api_key": "sk-a", "llm_model_name": "qwen-a"})
    db.upsert("B", {"llm_api_key": "sk-b", "llm_model_name": "qwen-b"})
    assert db.get("A")["llm_api_key"] == "sk-a"
    assert db.get("B")["llm_api_key"] == "sk-b"
