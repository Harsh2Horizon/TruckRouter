import hashlib
import io
import json
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import snapshot


class SnapshotTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "snapshot.json"
        self.patch = patch.multiple(snapshot, ROOT=self.root, CONFIG=self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def package(self, name="data/reference/test.txt", contents=b"frozen reference"):
        metadata = {
            "files": [
                {
                    "path": name,
                    "bytes": len(contents),
                    "sha256": hashlib.sha256(contents).hexdigest(),
                }
            ]
        }
        archive = self.root / "snapshot.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            for path, data in [
                (snapshot.MANIFEST, json.dumps(metadata).encode()),
                (name, contents),
            ]:
                info = tarfile.TarInfo(path)
                info.size = len(data)
                stream.addfile(info, io.BytesIO(data))
        self.config.write_text(
            json.dumps({"bytes": archive.stat().st_size, "sha256": snapshot.digest(archive)})
        )
        return archive

    def test_verified_snapshot_extracts_and_can_be_reused(self):
        archive = self.package()
        snapshot.fetch(archive)
        snapshot.fetch(archive)
        self.assertEqual((self.root / "data/reference/test.txt").read_bytes(), b"frozen reference")

    def test_corrupt_archive_is_rejected_before_extraction(self):
        archive = self.package()
        archive.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "verification failed"):
            snapshot.fetch(archive)
        self.assertFalse((self.root / "data").exists())

    def test_existing_different_cache_is_preserved(self):
        archive = self.package()
        target = self.root / "data/reference/test.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing local data")
        with self.assertRaisesRegex(ValueError, "differs"):
            snapshot.fetch(archive)
        self.assertEqual(target.read_bytes(), b"existing local data")

    def test_parent_traversal_is_rejected(self):
        archive = self.package("data/../../escape")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            snapshot.fetch(archive)

    def test_author_paths_become_checkout_relative(self):
        self.assertEqual(
            snapshot.portable(
                {"file": str(self.root / "data/test"), "url": "https://example.org/data"}
            ),
            {"file": "data/test", "url": "https://example.org/data"},
        )
