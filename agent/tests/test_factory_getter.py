import os, sys
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PROJECT_PARENT = os.path.dirname(PROJECT_ROOT)
for p in (PROJECT_ROOT, PROJECT_PARENT):
    if p not in sys.path:
        sys.path.insert(0, p)

import model.factory as fac

def test_reload_returns_new_instance_after_bump(monkeypatch):
    fac._config_version += 1
    a = fac.get_chat_model()
    # 模拟一次配置保存触发 reload：bump 版本号
    monkeypatch.setattr(fac, "_user_settings_override", {"llm_model_name": "qwen-max"})
    fac._config_version += 1
    b = fac.get_chat_model()
    assert b is not a  # 版本变了 → 新实例

def test_same_version_returns_same_instance(monkeypatch):
    fac._config_version += 1
    old = fac.get_chat_model()
    # 不 bump 版本，override 不变 → 同一实例
    assert fac.get_chat_model() is old
