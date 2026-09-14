import base64
import io
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import server


class TextToSpeechTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(server.app)

    def test_danish_page_exposes_google_tts_url(self):
        with patch.dict(os.environ, {"GOOGLE_TTS_API_KEY": "test-key"}):
            response = self.client.get("/api/pages/15?language=da")

        self.assertEqual(response.status_code, 200)
        entry = next(item for item in response.json()["entries"] if item["id"] == 41)
        self.assertEqual(entry["translation"], "en mund")
        self.assertRegex(entry["translation_audio"], r"^/api/tts/da/41\.mp3\?v=")

    @patch("server.synthesize_google_tts", return_value=b"ID3-audio")
    def test_tts_speaks_headword_without_noun_marker(self, synthesize):
        response = self.client.get("/api/tts/da/31.mp3")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/mpeg")
        self.assertEqual(response.content, b"ID3-audio")
        synthesize.assert_called_once_with("da", "læber")

    @patch("urllib.request.urlopen")
    def test_google_tts_uses_danish_male_voice(self, urlopen):
        audio = b"ID3-male-voice"
        urlopen.return_value = io.BytesIO(
            json.dumps(
                {"audioContent": base64.b64encode(audio).decode("ascii")}
            ).encode("utf-8")
        )
        server.synthesize_google_tts.cache_clear()

        with patch.dict(os.environ, {"GOOGLE_TTS_API_KEY": "test-key"}):
            result = server.synthesize_google_tts("da", "en mund")

        self.assertEqual(result, audio)
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["input"]["text"], "en mund")
        self.assertEqual(payload["voice"]["languageCode"], "da-DK")
        self.assertEqual(payload["voice"]["name"], "da-DK-Standard-G")

    def test_missing_google_api_key_returns_service_unavailable(self):
        server.synthesize_google_tts.cache_clear()
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.get("/api/tts/da/41.mp3")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "GOOGLE_TTS_API_KEY is not configured",
        )


if __name__ == "__main__":
    unittest.main()
