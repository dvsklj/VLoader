import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as vloader


class VLoaderTests(unittest.TestCase):
    def setUp(self):
        vloader.app.config.update(TESTING=True)
        self.client = vloader.app.test_client()
        with vloader.state_lock:
            vloader.active_downloads.clear()
            vloader.download_history.clear()

    def test_missing_impersonation_support_falls_back_cleanly(self):
        with patch.object(vloader, "get_impersonation_target", return_value=None):
            options = vloader.extract_options()
        self.assertNotIn("impersonate", options)
        self.assertNotIn("extractor_args", options)

    def test_requested_format_error_explains_how_to_update(self):
        message = vloader.friendly_download_error(Exception("Requested format is not available"))
        self.assertIn("pip install -U -r requirements.txt", message)
        self.assertIn("restart VLoader", message)

    def test_concrete_format_selection_merges_best_video_and_audio(self):
        info = {
            "formats": [
                {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
                {"format_id": "134", "ext": "mp4", "height": 360, "vcodec": "h264", "acodec": "none"},
                {"format_id": "243", "ext": "webm", "height": 360, "vcodec": "vp9", "acodec": "none"},
            ]
        }
        self.assertEqual(vloader.select_available_format(info, None), "243+140")

    def test_concrete_format_selection_respects_manual_height(self):
        info = {
            "formats": [
                {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "h264", "acodec": "aac"},
                {"format_id": "22", "ext": "mp4", "height": 720, "vcodec": "h264", "acodec": "aac"},
            ]
        }
        self.assertEqual(vloader.select_available_format(info, 360), "18")

    def test_qualities_are_deduplicated_sorted_and_keep_best_fps(self):
        qualities = vloader.available_qualities(
            {
                "formats": [
                    {"vcodec": "avc1", "height": 720, "fps": 30, "ext": "mp4"},
                    {"vcodec": "vp9", "height": 1080, "fps": 30, "ext": "webm"},
                    {"vcodec": "av1", "height": 1080, "fps": 60, "ext": "mp4"},
                    {"vcodec": "none", "ext": "m4a"},
                ]
            }
        )
        self.assertEqual([item["value"] for item in qualities], ["1080", "720"])
        self.assertEqual(qualities[0]["fps"], 60)
        self.assertEqual(qualities[0]["containers"], ["mp4", "webm"])

    def test_direct_media_without_dimensions_gets_source_choice(self):
        qualities = vloader.available_qualities(
            {"formats": [{"vcodec": None, "height": None, "ext": "mp4"}]}
        )
        self.assertEqual(qualities[0]["value"], "source")

    def test_format_endpoint_returns_extracted_qualities(self):
        info = {
            "id": "abc",
            "title": "Example",
            "formats": [{"vcodec": "avc1", "height": 720, "fps": 30, "ext": "mp4"}],
        }
        ydl = MagicMock()
        ydl.__enter__.return_value.extract_info.return_value = info
        with patch.object(vloader.yt_dlp, "YoutubeDL", return_value=ydl):
            response = self.client.post("/api/formats", json={"url": "https://example.com/watch"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["qualities"][0]["value"], "720")

    def test_download_endpoint_queues_auto_without_metadata_request(self):
        with patch.object(vloader.socketio, "start_background_task") as start:
            response = self.client.post(
                "/api/download",
                json={"url": "https://example.com/watch", "quality": "auto", "file_format": "auto"},
            )

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertIn(payload["job_id"], vloader.active_downloads)
        start.assert_called_once_with(
            vloader.download_video, payload["job_id"], "https://example.com/watch", None, "auto"
        )

    def test_download_endpoint_rejects_invalid_inputs(self):
        cases = [
            ({"url": "file:///etc/passwd"}, "valid http"),
            ({"url": "https://example.com", "quality": "huge"}, "valid quality"),
            ({"url": "https://example.com", "file_format": "exe"}, "Unsupported"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                response = self.client.post("/api/download", json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.get_json()["error"])

    def test_locate_output_file_honours_requested_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "Example [abc].mkv"
            output.touch()
            ydl = MagicMock()
            ydl.prepare_filename.return_value = str(Path(directory) / "Example [abc].webm")
            self.assertEqual(vloader.locate_output_file(ydl, {}, "mkv"), output.resolve())


if __name__ == "__main__":
    unittest.main()
