import sqlite3
from types import SimpleNamespace

from hermes_cli.status import show_status


def test_show_status_includes_tavily_key(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-1234567890abcdef")

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Tavily" in output
    assert "tvly...cdef" in output


def test_show_status_reports_project_env_fallback_and_state_db_sessions(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod

    hermes_home = tmp_path / ".hermes-home"
    hermes_home.mkdir()
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / ".env").write_text("OPENAI_BASE_URL=https://project.example/v1\n", encoding="utf-8")

    conn = sqlite3.connect(hermes_home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, source, started_at, ended_at) VALUES ('s1', 'cli', 1, NULL)"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(status_mod, "PROJECT_ROOT", project_root, raising=False)
    monkeypatch.setattr(status_mod, "get_env_path", lambda: hermes_home / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: hermes_home, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)

    show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert ".env file:" in output
    assert "exists (project directory fallback)" in output
    assert "Stored:       1 session(s) (state.db)" in output
    assert "Active:       0" not in output


def test_show_status_termux_gateway_section_skips_systemctl(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod
    import hermes_cli.auth as auth_mod
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setenv("TERMUX_VERSION", "0.118.3")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(auth_mod, "get_nous_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)

    def _unexpected_systemctl(*args, **kwargs):
        raise AssertionError("systemctl should not be called in the Termux status view")

    monkeypatch.setattr(status_mod.subprocess, "run", _unexpected_systemctl)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Manager:      Termux / manual process" in output
    assert "Start with:   hermes gateway" in output
    assert "systemd (user)" not in output


def test_show_status_reports_nous_auth_error(monkeypatch, capsys, tmp_path):
    from hermes_cli import status as status_mod
    import hermes_cli.auth as auth_mod
    import hermes_cli.gateway as gateway_mod

    monkeypatch.setattr(status_mod, "get_env_path", lambda: tmp_path / ".env", raising=False)
    monkeypatch.setattr(status_mod, "get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setattr(status_mod, "load_config", lambda: {"model": "gpt-5.4"}, raising=False)
    monkeypatch.setattr(status_mod, "resolve_requested_provider", lambda requested=None: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "resolve_provider", lambda requested=None, **kwargs: "openai-codex", raising=False)
    monkeypatch.setattr(status_mod, "provider_label", lambda provider: "OpenAI Codex", raising=False)
    monkeypatch.setattr(
        auth_mod,
        "get_nous_auth_status",
        lambda: {
            "logged_in": False,
            "portal_base_url": "https://portal.nousresearch.com",
            "access_expires_at": "2026-04-20T01:00:51+00:00",
            "agent_key_expires_at": "2026-04-20T04:54:24+00:00",
            "has_refresh_token": True,
            "error": "Refresh session has been revoked",
        },
        raising=False,
    )
    monkeypatch.setattr(auth_mod, "get_codex_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(auth_mod, "get_qwen_auth_status", lambda: {}, raising=False)
    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda exclude_pids=None: [], raising=False)

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Nous Portal   ✗ not logged in (run: hermes auth add nous --type oauth)" in output
    assert "Error:      Refresh session has been revoked" in output
    assert "Access exp:" in output
    assert "Key exp:" in output
