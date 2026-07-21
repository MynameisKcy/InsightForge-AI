import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

import model.factory as fac

def test_priority_user_over_env_over_yaml(monkeypatch):
    # 用户配置 = qwen-user, 环境变量 = qwen-env, YAML = qwen-yaml
    monkeypatch.setenv("CHAT_MODEL_NAME", "qwen-env")
    monkeypatch.setattr(fac, "rag_conf", {"chat_model_name": "qwen-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {"llm_model_name": "qwen-user"})
    assert fac._resolve_chat_model_name() == "qwen-user"

def test_fallback_to_env_when_no_user_setting(monkeypatch):
    monkeypatch.setenv("CHAT_MODEL_NAME", "qwen-env")
    monkeypatch.setattr(fac, "rag_conf", {"chat_model_name": "qwen-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {})
    assert fac._resolve_chat_model_name() == "qwen-env"

def test_fallback_to_yaml(monkeypatch):
    monkeypatch.delenv("CHAT_MODEL_NAME", raising=False)
    monkeypatch.setattr(fac, "rag_conf", {"chat_model_name": "qwen-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {})
    assert fac._resolve_chat_model_name() == "qwen-yaml"

def test_embedding_priority_user(monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL_NAME", "emb-env")
    monkeypatch.setattr(fac, "rag_conf", {"embedding_model_name": "emb-yaml"})
    monkeypatch.setattr(fac, "_user_settings_override", {"embedding_model_name": "emb-user"})
    assert fac._resolve_embedding_model_name() == "emb-user"
