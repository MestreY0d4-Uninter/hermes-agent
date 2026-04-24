"""Tests for execution receipts delegate integration."""

import json
import time
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def isolated_hermes_home(tmp_path, monkeypatch):
    """Use a temporary HERMES_HOME for receipt tests."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # Also patch get_hermes_home to return our temp dir
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_home)
    return hermes_home


class TestDelegateReceiptIntegration:
    """Test that delegation automatically creates receipts."""

    def test_run_single_child_creates_receipt(self, isolated_hermes_home):
        """Running a child task should automatically create a receipt."""
        from unittest.mock import MagicMock
        from tools.execution_receipts import list_receipts

        mock_child = MagicMock()
        mock_child.session_id = "test_session_123"
        mock_child.tool_progress_callback = None
        mock_child.run_conversation.return_value = {
            "final_response": "Task completed successfully",
            "completed": True,
            "interrupted": False,
            "api_calls": 3,
            "messages": [],
        }
        mock_child._credential_pool = None
        mock_child._delegate_saved_tool_names = []
        mock_child.get_activity_summary.return_value = {}

        from tools.delegate_tool import _run_single_child
        result = _run_single_child(
            task_index=0,
            goal="Test goal",
            child=mock_child,
            parent_agent=None,
        )

        assert result["status"] == "completed"
        receipts = list_receipts(limit=10)
        assert len(receipts) >= 1
        r = receipts[0]
        assert r["status"] == "completed"
        assert r["execution_path"] == "delegation"

    def test_run_single_child_error_creates_failed_receipt(self, isolated_hermes_home):
        """A child that throws should create a failed receipt."""
        from unittest.mock import MagicMock
        from tools.execution_receipts import list_receipts

        mock_child = MagicMock()
        mock_child.session_id = "error_session"
        mock_child.run_conversation.side_effect = RuntimeError("something broke")
        mock_child._credential_pool = None
        mock_child._delegate_saved_tool_names = []

        from tools.delegate_tool import _run_single_child
        result = _run_single_child(
            task_index=0,
            goal="This will fail",
            child=mock_child,
            parent_agent=None,
        )

        assert result["status"] == "error"
        receipts = list_receipts(limit=10)
        assert len(receipts) >= 1
        assert receipts[0]["status"] == "failed"
        assert "something broke" in receipts[0].get("error", "")
