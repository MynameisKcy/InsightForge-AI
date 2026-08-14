import json
import agent.tools.agent_tools as tools

def test_list_user_files_uses_current_user_id(monkeypatch):
    """list_user_files 应按 _current_user_id() 取当前用户，A 的调用只走 A 的列表；
    同时返回 JSON 含 text/table 两类。"""
    seen = []
    def _stub_text(uid):
        seen.append(("text", uid))
        return [{"name": f"{uid}.pdf"}]
    def _stub_table(uid):
        seen.append(("table", uid))
        return [{"name": f"{uid}.csv", "table_name": uid}]
    monkeypatch.setattr(tools, "_current_user_id", lambda: "A")
    monkeypatch.setattr(tools, "_list_text_files", _stub_text)
    monkeypatch.setattr(tools, "_list_table_files", _stub_table)
    out = tools.list_user_files.invoke({})
    data = json.loads(out)
    # 含两类
    assert any(f["type"] == "text" for f in data)
    assert any(f["type"] == "table" for f in data)
    # A 的调用只见到 A 的文件
    assert all(f["name"].startswith("A") for f in data)
    # 列表函数被调用时传入的是 A，不是别的用户
    assert all(uid == "A" for _, uid in seen)

def test_list_user_files_b_does_not_see_a(monkeypatch):
    """切换为 B 后，不应见到 A 的文件。"""
    monkeypatch.setattr(tools, "_current_user_id", lambda: "B")
    monkeypatch.setattr(tools, "_list_text_files", lambda uid: [{"name": f"{uid}.pdf"}])
    monkeypatch.setattr(tools, "_list_table_files", lambda uid: [{"name": f"{uid}.csv", "table_name": uid}])
    out = tools.list_user_files.invoke({})
    data = json.loads(out)
    assert all(f["name"].startswith("B") for f in data)
