import pytest

from trader.config import AppConfig, Secrets, load_config, save_config


def test_defaults_match_investor_decisions():
    c = AppConfig()
    assert c.mode == "testnet"
    assert c.risk.max_daily_loss_pct == 2.0  # D-06
    assert c.risk.max_drawdown_pct == 20.0  # D-07
    assert c.risk.max_position_pct_per_coin == 10.0  # D-08
    assert c.risk.max_open_positions == 5  # D-08
    assert c.screener.quote_asset == "USDT"  # D-10
    assert c.trading_window == "24/7"  # D-12
    assert c.fee_rate_per_side == 0.001  # real-fee modeling on zero-fee testnet


def test_risk_limits_can_tighten_but_not_loosen():
    AppConfig(risk={"max_daily_loss_pct": 1.0})  # tighter: fine
    with pytest.raises(ValueError, match="looser than the non-negotiable floor"):
        AppConfig(risk={"max_daily_loss_pct": 5.0})
    with pytest.raises(ValueError, match="looser"):
        AppConfig(risk={"max_open_positions": 10})


def test_mode_is_validated():
    with pytest.raises(ValueError):
        AppConfig(mode="yolo")


def test_yaml_roundtrip(tmp_path):
    path = tmp_path / "config.yaml"
    original = AppConfig(risk={"max_daily_loss_pct": 1.5})
    save_config(original, path)
    loaded = load_config(path)
    assert loaded == original


def test_missing_file_gives_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml") == AppConfig()


def test_secrets_from_env(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "k")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "s")
    s = Secrets.from_env("testnet").require()
    assert s.binance_api_key == "k"

    monkeypatch.delenv("BINANCE_TESTNET_API_KEY")
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET")
    with pytest.raises(RuntimeError, match="credentials missing"):
        Secrets.from_env("testnet").require()
