from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.services import provider_gateway


class FakeResponse:
    def __init__(self, *, status_code=200, json_data=None, content=b"", content_type="application/json"):
        self.status_code = status_code
        self._json_data = json_data
        self.content = content
        self.text = json.dumps(json_data) if json_data is not None else ""
        self.headers = {"content-type": content_type}

    def json(self):
        return self._json_data


class ProviderGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tempdir.name) / "providers.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "providers": {
                        "image-local": {
                            "kind": "image",
                            "protocol": "mpt_image_v1",
                            "enabled": True,
                            "base_url": "http://provider.local:8000",
                            "auth": {
                                "type": "bearer_env",
                                "env": "TEST_PROVIDER_TOKEN",
                                "optional": True,
                            },
                            "actions": {
                                "health": {"method": "GET", "path": "/health", "timeout_s": 10},
                                "generate_image": {"method": "POST", "path": "/v1/images", "timeout_s": 10},
                            },
                            "defaults": {
                                "save_dir": "moneyprinterturbo",
                                "width": 1024,
                                "height": 1024,
                                "min_size": 512,
                                "max_size": 1536,
                                "size_multiple": 16,
                                "files_path": "/files/",
                                "negative_prompt": "text, watermark",
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.env = mock.patch.dict(
            os.environ,
            {
                "MPT_PROVIDER_CONFIG": str(self.config_path),
                "TEST_PROVIDER_TOKEN": "super-secret",
            },
            clear=False,
        )
        self.env.start()
        provider_gateway.load_registry(force_reload=True)

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def test_discovery_does_not_expose_secret(self):
        public = provider_gateway.list_providers()
        self.assertEqual(public[0]["id"], "image-local")
        self.assertNotIn("super-secret", json.dumps(public))
        self.assertTrue(public[0]["auth"]["configured"])

    def test_unknown_action_is_rejected_before_network(self):
        with mock.patch("app.services.provider_gateway.requests.request") as request:
            with self.assertRaises(provider_gateway.ProviderGatewayError):
                provider_gateway.invoke("image-local", "not-declared")
        request.assert_not_called()

    @mock.patch("app.services.provider_gateway.requests.request")
    def test_health_uses_declared_endpoint(self, request):
        request.return_value = FakeResponse(json_data={"ok": True})
        result = provider_gateway.health("image-local")
        self.assertTrue(result["ok"])
        args, kwargs = request.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "http://provider.local:8000/health")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer super-secret")

    @mock.patch("app.services.provider_gateway.requests.get")
    @mock.patch("app.services.provider_gateway.requests.request")
    def test_openai_image_bridge_translates_native_provider(self, request, get):
        request.return_value = FakeResponse(
            json_data={
                "model": "FLUX.local",
                "relative_path": "jobs/test/image.png",
                "seed": 42,
                "elapsed_seconds": 1.2,
            }
        )
        get.return_value = FakeResponse(content=b"PNGDATA", content_type="image/png")
        result = provider_gateway.openai_image_generation(
            "image-local",
            {
                "model": "provider-default",
                "prompt": "technical diagram",
                "n": 1,
                "size": "1536x1024",
            },
        )
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["prompt"], "technical diagram")
        self.assertEqual(payload["width"], 1536)
        self.assertEqual(payload["height"], 1024)
        self.assertEqual(payload["negative_prompt"], "text, watermark")
        self.assertEqual(
            get.call_args.args[0],
            "http://provider.local:8000/files/jobs/test/image.png",
        )
        self.assertEqual(result["data"][0]["b64_json"], "UE5HREFUQQ==")


if __name__ == "__main__":
    unittest.main()
