import gzip
import tempfile
import unittest
from pathlib import Path

from src.storage import create_upload_batch, decode_log_bytes, load_upload_events


class DecodeLogBytesTests(unittest.TestCase):
    def test_decodes_plain_text_bytes(self):
        self.assertEqual(decode_log_bytes(b"hello world", "mongod.log"), "hello world")

    def test_decompresses_gzip_bytes_by_extension(self):
        compressed = gzip.compress(b"hello gzip world")
        self.assertEqual(decode_log_bytes(compressed, "mongod.log.1.gz"), "hello gzip world")

    def test_raises_value_error_for_invalid_gzip_content(self):
        with self.assertRaises(ValueError):
            decode_log_bytes(b"not actually gzip", "mongod.log.gz")


class CreateUploadBatchGzipTests(unittest.TestCase):
    def test_parses_mixed_plain_and_gzip_mongodb_logs(self):
        plain_line = (
            '{"ts":"2026-01-01T10:00:00Z","durationMillis":240,"ns":"shop.orders",'
            '"command":{"find":"orders","filter":{"status":"PENDING"}}}\n'
        )
        gzip_line = (
            '{"ts":"2026-01-01T10:00:01Z","durationMillis":300,"ns":"shop.orders",'
            '"command":{"find":"orders","filter":{"status":"SHIPPED"}}}\n'
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            plain_file = tmp_path / "mongod.log"
            plain_file.write_text(plain_line, encoding="utf-8")

            gzip_file = tmp_path / "mongod.log.1.gz"
            gzip_file.write_bytes(gzip.compress(gzip_line.encode("utf-8")))

            upload_dir = tmp_path / "uploads"
            manifest = create_upload_batch(
                files=[str(plain_file), str(gzip_file)],
                db_type="MongoDB",
                db_version="8.0",
                upload_dir=str(upload_dir),
            )

            self.assertEqual(manifest["event_count"], 2)
            events = load_upload_events(manifest["id"], upload_dir=str(upload_dir))
            self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
