from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gold_trader.infra.secrets import (
    load_secrets,
    resolve_bridge_secret,
    resolve_openai_api_key,
    save_secrets,
    secrets_status,
)


class SecretsTests(unittest.TestCase):
    def test_save_and_load_openai_key(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "secrets.json"
            save_secrets({"openai_api_key": "sk-test-key-1234"}, path=p)
            loaded = load_secrets(p)
            self.assertEqual(loaded["openai_api_key"], "sk-test-key-1234")

    def test_env_overrides_file(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "secrets.json"
            save_secrets({"openai_api_key": "sk-from-file"}, path=p)
            with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-env"}, clear=False):
                self.assertEqual(resolve_openai_api_key(path=p), "sk-from-env")

    def test_clear_key(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "secrets.json"
            save_secrets({"openai_api_key": "sk-old"}, path=p)
            save_secrets({"clear_openai_api_key": True}, path=p)
            self.assertEqual(load_secrets(p), {})

    def test_bridge_secret_fallback_chain(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "secrets.json"
            save_secrets({"bridge_secret": "secret-file"}, path=p)
            with patch.dict(os.environ, {}, clear=True):
                os.environ.pop("GOLD_BRIDGE_SECRET", None)
                self.assertEqual(resolve_bridge_secret(path=p, runtime_fallback="runtime"), "secret-file")
                self.assertEqual(resolve_bridge_secret(path=p, runtime_fallback=""), "secret-file")

    def test_status_never_exposes_full_key(self) -> None:
        with TemporaryDirectory() as td:
            p = Path(td) / "secrets.json"
            save_secrets({"openai_api_key": "sk-abcdefghijklmnop"}, path=p)
            status = secrets_status(path=p)
            self.assertTrue(status["openai_api_key_set"])
            self.assertEqual(status["openai_api_key_hint"], "…mnop")
            self.assertNotIn("abcdefghijklmnop", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
