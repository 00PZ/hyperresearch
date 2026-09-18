"""[web] search_provider / fetch_provider load-save and start-gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyperresearch.core.config import VaultConfig
from hyperresearch.web.searxng import resolve_searxng_url, validate_search_config


def _web_section(text: str) -> str:
    after = text.split("[web]", 1)[1]
    nxt = after.find("\n[")
    return after if nxt < 0 else after[:nxt]


def test_absent_search_provider_defaults_none(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[web]\nprovider = "builtin"\n', encoding="utf-8")
    cfg = VaultConfig.load(p)
    assert cfg.search_provider == "none"
    assert validate_search_config(cfg) is None


def test_missing_file_search_provider_none(tmp_path: Path) -> None:
    cfg = VaultConfig.load(tmp_path / "nope.toml")
    assert cfg.search_provider == "none"


def test_fetch_provider_wins_over_deprecated_provider(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        '[web]\nprovider = "tavily"\nfetch_provider = "builtin"\n',
        encoding="utf-8",
    )
    cfg = VaultConfig.load(p)
    assert cfg.web_provider == "builtin"


def test_provider_alone_is_fetch_provider(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[web]\nprovider = "crawl4ai"\n', encoding="utf-8")
    cfg = VaultConfig.load(p)
    assert cfg.web_provider == "crawl4ai"
    assert cfg.search_provider == "none"


def test_save_omits_provider_writes_new_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    cfg = VaultConfig(search_provider="none", web_provider="builtin", searxng_url="")
    cfg.save(p)
    text = p.read_text(encoding="utf-8")
    web = _web_section(text)
    assert 'search_provider = "none"' in web
    assert 'fetch_provider = "builtin"' in web
    assert "searxng_url" in web
    assert "searxng_trip_log" in web
    assert "\nprovider =" not in web
    assert not web.lstrip().startswith("provider =")
    loaded = VaultConfig.load(p)
    assert loaded.search_provider == "none"
    assert loaded.web_provider == "builtin"


def test_roundtrip_identity_includes_search_keys(tmp_path: Path) -> None:
    cfg = VaultConfig()
    p = tmp_path / "config.toml"
    cfg.save(p)
    assert VaultConfig.load(p) == cfg


def test_unset_searxng_url_leaves_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    cfg = VaultConfig(search_provider="searxng", searxng_url="http://lan:8080")
    assert resolve_searxng_url(cfg) == "http://lan:8080"


def test_set_empty_env_overrides_to_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "")
    cfg = VaultConfig(search_provider="searxng", searxng_url="http://lan:8080")
    assert resolve_searxng_url(cfg) == ""
    assert validate_search_config(cfg) == "searxng_unconfigured"


def test_unknown_search_provider_is_searxng_config() -> None:
    cfg = VaultConfig(search_provider="tavily")
    assert validate_search_config(cfg) == "searxng_config"


def test_fetch_provider_searxng_is_searxng_config() -> None:
    cfg = VaultConfig(search_provider="none", web_provider="searxng")
    assert validate_search_config(cfg) == "searxng_config"


@pytest.mark.parametrize(
    "url",
    ["ftp://x", "http://user:pass@127.0.0.1:8080", "http://user@host", "not-a-url"],
)
def test_bad_scheme_or_userinfo_is_searxng_config(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    cfg = VaultConfig(search_provider="searxng", searxng_url=url)
    assert validate_search_config(cfg) == "searxng_config"


def test_blank_url_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    cfg = VaultConfig(search_provider="searxng", searxng_url="  ")
    assert validate_search_config(cfg) == "searxng_unconfigured"


def test_install_writes_explicit_search_and_fetch_providers(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from hyperresearch.cli import app

    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["install", str(tmp_path), "--json", "-n", "T"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / ".hyperresearch" / "config.toml").read_text(encoding="utf-8")
    web = _web_section(text)
    assert 'search_provider = "searxng"' in web
    assert 'fetch_provider = "crawl4ai"' in web
    assert "\nprovider =" not in web
