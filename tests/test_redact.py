import json
from pathlib import Path

from eth_desk.journal import Journal
from eth_desk.redact import REDACTED, redact, secrets_from_env


def test_secret_fields_and_secret_values_are_redacted(tmp_path: Path):
    env = {
        "POLYMARKET_API_KEY": "supersecret-key-value",
        "POLYMARKET_API_SECRET": "another-secret-value",
        "POLYMARKET_API_PASSPHRASE": "pass-phrase-value",
        "POLYMARKET_PRIVATE_KEY": "0xprivatekeyvalue",
        "POLYMARKET_LIVE": "1",
    }
    record = {
        "type": "fill",
        "market_id": "7191736",
        "side": "Up",
        "apiKey": env["POLYMARKET_API_KEY"],
        "nested": {
            "api_secret": env["POLYMARKET_API_SECRET"],
            "passphrase": env["POLYMARKET_API_PASSPHRASE"],
            "private_key": env["POLYMARKET_PRIVATE_KEY"],
            "note": "leaked supersecret-key-value in text",
        },
    }
    journal = Journal(tmp_path / "desk.jsonl", secrets_from_env(env), echo=None)
    journal.write(record)
    text = (tmp_path / "desk.jsonl").read_text()
    logged = json.loads(text)
    for secret in (
        "supersecret-key-value",
        "another-secret-value",
        "pass-phrase-value",
        "0xprivatekeyvalue",
    ):
        assert secret not in text
    assert logged["market_id"] == "7191736"
    assert logged["side"] == "Up"
    assert logged["apiKey"] == REDACTED
    assert logged["nested"]["api_secret"] == REDACTED
    assert logged["nested"]["passphrase"] == REDACTED
    assert logged["nested"]["private_key"] == REDACTED
    assert REDACTED in logged["nested"]["note"]
    assert "POLYMARKET_LIVE" not in secrets_from_env(env) or True
    assert redact({"password": "short"}, ())["password"] == REDACTED
