import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


class TestLiveCdpTaskIsolation:
    def test_same_task_reuses_live_cdp_session(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "ws://host:9222/devtools/browser/abc")

        s1 = browser_tool._get_session_info("task-a")
        s2 = browser_tool._get_session_info("task-a")

        assert s1 is s2
        assert s1["cdp_url"] == "ws://host:9222/devtools/browser/abc"

    def test_second_live_cdp_task_is_rejected(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "ws://host:9222/devtools/browser/abc")

        s1 = browser_tool._get_session_info("task-a")
        assert s1["cdp_url"] == "ws://host:9222/devtools/browser/abc"

        with pytest.raises(RuntimeError) as exc:
            browser_tool._get_session_info("task-b")

        msg = str(exc.value)
        assert "shared-state" in msg
        assert "task-a" in msg

    def test_concurrent_live_cdp_tasks_are_atomically_rejected(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "ws://host:9222/devtools/browser/abc")
        monkeypatch.setattr(browser_tool, "_ensure_cdp_supervisor", lambda task_id: None)

        create_started = threading.Event()
        release_create = threading.Event()
        created_tasks = []

        def slow_create_cdp_session(task_id, cdp_url):
            created_tasks.append(task_id)
            create_started.set()
            assert release_create.wait(2), "timed out waiting to release test CDP session creation"
            return {
                "session_name": f"cdp_{task_id}",
                "bb_session_id": None,
                "cdp_url": cdp_url,
                "features": {"cdp_override": True},
            }

        monkeypatch.setattr(browser_tool, "_create_cdp_session", slow_create_cdp_session)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(browser_tool._get_session_info, "task-a")
            assert create_started.wait(2), "first task never started CDP session creation"
            second = pool.submit(browser_tool._get_session_info, "task-b")
            release_create.set()

            outcomes = []
            for future in (first, second):
                try:
                    outcomes.append(("ok", future.result(timeout=2)))
                except RuntimeError as exc:
                    outcomes.append(("error", str(exc)))

        assert [kind for kind, _ in outcomes].count("ok") == 1
        assert [kind for kind, _ in outcomes].count("error") == 1
        error = next(value for kind, value in outcomes if kind == "error")
        assert "shared-state" in error
        assert len(created_tasks) == 1
        assert len(browser_tool._active_sessions) == 1

    def test_non_cdp_sessions_can_still_coexist(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "")
        monkeypatch.setattr(browser_tool, "_get_cloud_provider", lambda: None)

        s1 = browser_tool._get_session_info("task-a")
        s2 = browser_tool._get_session_info("task-b")

        assert s1["session_name"] != s2["session_name"]
        assert not s1.get("cdp_url")
        assert not s2.get("cdp_url")

    def test_live_cdp_task_does_not_treat_generic_cdp_cloud_session_as_conflict(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {
            "cloud-task": {
                "session_name": "cloud-session",
                "cdp_url": "ws://cloud.example/devtools/browser/abc",
                "features": {"browserbase": True},
            }
        })
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "ws://host:9222/devtools/browser/abc")

        session = browser_tool._get_session_info("live-task")
        assert session["features"]["cdp_override"] is True

    def test_browser_navigate_returns_json_error_for_second_live_cdp_task(self, monkeypatch):
        import tools.browser_tool as browser_tool

        monkeypatch.setattr(browser_tool, "_active_sessions", {})
        monkeypatch.setattr(browser_tool, "_session_last_activity", {})
        monkeypatch.setattr(browser_tool, "_start_browser_cleanup_thread", lambda: None)
        monkeypatch.setattr(browser_tool, "_update_session_activity", lambda task_id: None)
        monkeypatch.setattr(browser_tool, "_get_cdp_override", lambda: "ws://host:9222/devtools/browser/abc")

        first = browser_tool._get_session_info("task-a")
        assert first["cdp_url"]

        result = json.loads(browser_tool.browser_navigate("https://example.com", task_id="task-b"))
        assert result["success"] is False
        assert "shared-state" in result["error"]
