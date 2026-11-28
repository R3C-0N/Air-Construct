"""Claude control: the MCP server (stdio protocol), the bridge and its commands."""

from __future__ import annotations

import base64
import io
import json
import threading

import pytest

from airconstruct.assistant import protocol
from airconstruct.assistant.mcp_server import TOOLS, McpServer, Tools


# ----------------------------------------------------------------------------- MCP side
class FakeClient:
    """Stands for BridgeClient: records calls, answers from a table."""

    path = None

    def __init__(self, answers=None, fail=None):
        self.calls = []
        self.answers = answers or {}
        self.fail = fail

    def call(self, method, **params):
        self.calls.append((method, params))
        if self.fail is not None:
            raise self.fail
        ans = self.answers.get(method, {"ok": True})
        return ans(params) if callable(ans) else ans

    def connected(self):
        return self.fail is None


def _rpc(server, method, params=None, mid=1):
    return server.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                          "params": params or {}})


def test_initialize_and_list_tools():
    s = McpServer(Tools(FakeClient()))
    r = _rpc(s, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "1"}})
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in r["result"]["capabilities"]
    assert "Air-Construct" in r["result"]["instructions"]
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = _rpc(s, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"screenshot", "add_part", "add_opening", "run_simulation", "simulation_status",
            "get_results", "update_case"} <= names
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        json.dumps(t)  # serialisable
    assert _rpc(s, "initialize", {"protocolVersion": "1999-01-01"})["result"][
        "protocolVersion"] == "2025-06-18"


def test_every_tool_is_a_bridge_command(qapp, tmp_path, settings):
    from ui_support import make_window

    win = make_window(tmp_path, settings)
    table = win.assistant.commands.table
    for t in TOOLS:
        if t["name"] != "simulation_status":
            assert t["name"] in table, t["name"]
    assert "run_status" in table


def test_tool_call_forwards_and_reports_errors():
    client = FakeClient({"add_part": {"name": "cpu2", "valid": True}})
    s = McpServer(Tools(client))
    r = _rpc(s, "tools/call", {"name": "add_part", "arguments": {"size": [0.01, 0.01, 0.01]}})
    assert r["result"]["isError"] is False
    assert json.loads(r["result"]["content"][0]["text"])["name"] == "cpu2"
    assert client.calls == [("add_part", {"size": [0.01, 0.01, 0.01]})]

    client.fail = protocol.BridgeError("the model is locked")
    r = _rpc(s, "tools/call", {"name": "add_part", "arguments": {}})
    assert r["result"]["isError"] is True
    assert "locked" in r["result"]["content"][0]["text"]

    r = _rpc(s, "tools/call", {"name": "nope"})
    assert r["error"]["code"] == -32602
    assert _rpc(s, "nope/nope")["error"]["code"] == -32601


def test_screenshot_is_an_image_block():
    png = base64.b64encode(b"\x89PNG fake").decode()
    s = McpServer(Tools(FakeClient({"screenshot": {"png_base64": png, "width": 10,
                                                   "height": 5, "tab": "model"}})))
    r = _rpc(s, "tools/call", {"name": "screenshot", "arguments": {"target": "view"}})
    img, meta = r["result"]["content"]
    assert img == {"type": "image", "data": png, "mimeType": "image/png"}
    assert json.loads(meta["text"])["width"] == 10


def test_simulation_status_waits_for_the_end():
    states = iter([{"running": True}, {"running": True}, {"running": False, "outcome": "ok"}])
    client = FakeClient({"run_status": lambda _p: next(states)})
    tools = Tools(client, sleep=lambda _s: None)
    out = tools.run("simulation_status", {"wait_seconds": 60})
    assert json.loads(out[0]["text"])["outcome"] == "ok"
    assert [c[0] for c in client.calls] == ["run_status"] * 3
    # no wait: one look
    client.calls.clear()
    states = iter([{"running": True}])
    client.answers["run_status"] = lambda _p: next(states)
    tools.run("simulation_status", {})
    assert len(client.calls) == 1


def test_app_started_when_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv(protocol.ENV_FILE, str(tmp_path / "bridge.json"))
    launched = []

    class Client(FakeClient):
        def call(self, method, **params):
            if not launched:
                raise protocol.NotRunning("no app")
            return super().call(method, **params)

    tools = Tools(Client(), launcher=lambda: launched.append(1), sleep=lambda _s: None)
    assert json.loads(tools.run("get_state", {})[0]["text"]) == {"ok": True}
    assert launched == [1]


def test_serve_reads_and_writes_lines():
    s = McpServer(Tools(FakeClient()))
    inp = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        "not json",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]) + "\n")
    out = io.StringIO()
    s.serve(inp, out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, None, 2]
    assert replies[1]["error"]["code"] == -32700


# ----------------------------------------------------------------------------- app side
@pytest.fixture
def win(qapp, tmp_path, settings, monkeypatch):
    from ui_support import fake_run, fake_summary, make_window

    monkeypatch.setenv(protocol.ENV_FILE, str(tmp_path / "bridge.json"))
    w = make_window(tmp_path, settings, run_fn=fake_run, summarize_fn=fake_summary,
                    load_regions_fn=lambda d: {}, preview_fn=lambda c: {"cpu": 61.0})
    assert w.assistant.start()
    yield w
    w.assistant.stop()


def cmd(win, method, **params):
    """One request through the bridge's line handler, as the socket does."""
    line = json.dumps({"id": 1, "token": win.assistant.token, "method": method,
                       "params": params}).encode()
    reply = json.loads(win.assistant.handle_line(line))
    if "error" in reply:
        raise protocol.BridgeError(reply["error"])
    return reply["result"]


def test_bridge_file_and_token(win, tmp_path):
    info = protocol.read_bridge_file()
    assert info["port"] == win.assistant.port and info["token"] == win.assistant.token
    bad = json.dumps({"id": 1, "token": "wrong", "method": "ping"}).encode()
    assert win.assistant.handle_line(bad) is None
    assert win.assistant.handle_line(b"garbage") is None
    win.assistant.stop()
    assert protocol.read_bridge_file() is None


def test_add_and_change_parts_show_in_the_window(win):
    r = cmd(win, "add_part", name="dcdc", size=[0.02, 0.02, 0.01], power=4,
            material="aluminium", max_temperature=100)
    assert r["changed"] and r["valid"] and r["name"] == "dcdc"
    assert win.current_tab.key == "model"
    assert win.selection.keys == ["component:dcdc"]
    assert win.project.undo_stack.undoText().startswith("Claude · ")
    assert any(c["name"] == "dcdc" for c in win.project.get("components"))
    # visible to the user: status message and pill
    assert "Claude" in win.status.currentMessage()
    assert win.assistant_indicator.state == "active"
    assert "dcdc" in win.assistant_indicator.text()

    r = cmd(win, "update_part", name="dcdc", changes={"name": "dcdc2", "power": 6,
                                                       "origin": [0.02, 0.02, 0.007]})
    c = next(c for c in win.project.get("components") if c["name"] == "dcdc2")
    assert c["power"] == 6 and c["box"]["origin"] == {"x": 0.02, "y": 0.02, "z": 0.007}
    assert win.selection.keys == ["component:dcdc2"]

    with pytest.raises(protocol.BridgeError, match="no part"):
        cmd(win, "update_part", name="ghost", changes={"power": 1})
    with pytest.raises(protocol.BridgeError, match="cannot rename"):
        cmd(win, "update_part", name="dcdc2", changes={"name": "cpu"})

    cmd(win, "delete", kind="part", name="dcdc2")
    assert "dcdc2" not in [c["name"] for c in win.project.get("components")]
    r = cmd(win, "undo")
    assert r["done"] and "dcdc2" in [c["name"] for c in win.project.get("components")]


def test_openings_case_settings_probes(win):
    r = cmd(win, "add_opening", kind="outlet", face="zmax", name="vent",
            size=[0.04, 0.02], grille={"open_area": 0.6})
    assert r["valid"], r
    o = next(o for o in win.project.get("openings") if o["name"] == "vent")
    assert o["face"] == "zmax" and o["grille"] == {"open_area": 0.6}
    assert win.current_tab.key == "air" and win.selection.keys == ["opening:vent"]

    cmd(win, "update_opening", name="inlet", changes={"fan": {"flow_rate": 0.004}})
    o = next(o for o in win.project.get("openings") if o["name"] == "inlet")
    assert o["fan"] == {"flow_rate": 0.004} and o["flow_rate"] is None

    r = cmd(win, "update_case", changes={"ambient_temperature": 35, "precision": {"level": 1},
                                         "enclosure": [0.25, 0.12, 0.06]})
    assert win.project.get("ambient_temperature") == 35
    assert win.project.get("precision", "level") == 1
    assert win.project.get("enclosure") == {"x": 0.25, "y": 0.12, "z": 0.06}
    with pytest.raises(protocol.BridgeError, match="cannot change"):
        cmd(win, "update_case", changes={"components": []})

    cmd(win, "add_probe", name="p1", point=[0.1, 0.06, 0.03])
    cmd(win, "add_refinement", target="cpu", factor=3)
    assert win.project.get("refinements")[-1]["target"] == "cpu"
    cmd(win, "add_material", name="alu_anodise", kappa=180, rho=2700, cp=900)
    cmd(win, "add_part", name="hs", size=[0.02, 0.02, 0.01], material="alu_anodise")
    assert cmd(win, "get_state")["valid"]

    # an edit that makes the case invalid is applied and reported (undo fixes it)
    r = cmd(win, "update_part", name="hs", changes={"origin": [1, 1, 1]})
    assert r["valid"] is False and "outside" in r["validation_error"]


def test_get_model_and_state(win):
    m = cmd(win, "get_model")
    assert m["case"]["name"] == "electronics_box" and "ui" not in m["case"]
    assert "aluminium" in m["built_in_materials"]
    st = cmd(win, "get_state")
    assert st["tab"] == "model" and st["valid"] and st["run"]["running"] is False
    assert set(st["tabs"]) == {"model", "air", "volume", "simulation", "results"}


def test_screenshot_png(win):
    r = cmd(win, "screenshot", tab="air")
    raw = base64.b64decode(r["png_base64"])
    assert raw[:8] == b"\x89PNG\r\n\x1a\n" and r["width"] > 100
    assert win.current_tab.key == "air"
    r = cmd(win, "screenshot", target="view")
    assert base64.b64decode(r["png_base64"])[:4] == b"\x89PNG"


def test_pause_refuses_actions_but_not_reads(win):
    win.assistant.set_paused(True)
    assert win.assistant_indicator.state == "paused"
    with pytest.raises(protocol.BridgeError, match="paused"):
        cmd(win, "add_part", size=[0.01, 0.01, 0.01])
    assert cmd(win, "get_state")["valid"]
    win.assistant.set_paused(False)
    cmd(win, "add_part", size=[0.01, 0.01, 0.01])


def test_unsaved_changes_are_protected(win, tmp_path):
    cmd(win, "add_part", size=[0.01, 0.01, 0.01])
    with pytest.raises(protocol.BridgeError, match="unsaved"):
        cmd(win, "new_project", template="rack")
    path = tmp_path / "mine.yaml"
    assert cmd(win, "save_project", path=str(path))["path"] == str(path)
    assert path.exists() and not win.project.dirty
    r = cmd(win, "new_project", template="rack", name="rack_test")
    assert r["name"] == "rack_test" and r["valid"]
    r = cmd(win, "open_project", path=str(path))
    assert r["name"] == "electronics_box"


def test_run_estimate_and_results(win, qapp):
    from ui_support import wait_until

    est = cmd(win, "quick_estimate")
    assert est["temperatures_c"] == {"cpu": 61.0} and est["estimated_run_time_s"] == 1.0
    with pytest.raises(protocol.BridgeError, match="no results"):
        cmd(win, "get_results")
    st = cmd(win, "run_simulation")
    assert win.current_tab.key == "simulation"
    if st["running"]:
        with pytest.raises(protocol.BridgeError, match="locked|already"):
            cmd(win, "add_part", size=[0.01, 0.01, 0.01])
    assert wait_until(qapp, lambda: not win.runner.running and win.project.results is not None)
    st = cmd(win, "run_status")
    assert st["outcome"]["success"] is True
    res = cmd(win, "get_results")
    assert win.current_tab.key == "results"
    cpu = next(c for c in res["summary"]["components"] if c["name"] == "cpu")
    assert cpu["t_max_c"] == pytest.approx(55.0) and "over_limit" in cpu
    json.dumps(res, allow_nan=False)  # strict JSON
    assert res["stale"] is False
    cmd(win, "update_part", name="cpu", changes={"power": 20})
    assert cmd(win, "get_results")["stale"] is True


def test_real_socket_round_trip(win, qapp):
    """BridgeClient in a thread (as the MCP server process) against the window."""
    from ui_support import wait_until

    out = {}

    def work():
        client = protocol.BridgeClient(timeout=10)
        try:
            out["state"] = client.call("get_state")
            out["part"] = client.call("add_part", name="socket_part", size=[0.01, 0.01, 0.01])
            try:
                client.call("delete", kind="part", name="ghost")
            except protocol.BridgeError as exc:
                out["error"] = str(exc)
        finally:
            client.close()

    t = threading.Thread(target=work)
    t.start()
    assert wait_until(qapp, lambda: not t.is_alive(), 15)
    t.join()
    assert out["state"]["project"]["name"] == "electronics_box"
    assert out["part"]["name"] == "socket_part"
    assert "ghost" in out["error"]
    assert "socket_part" in [c["name"] for c in win.project.get("components")]


def test_menu_switch_turns_control_off(win):
    win.set_assistant_allowed(False)
    assert not win.assistant.enabled and protocol.read_bridge_file() is None
    win.set_assistant_allowed(True)
    assert win.assistant.enabled and protocol.read_bridge_file() is not None
