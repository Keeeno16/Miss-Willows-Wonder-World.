"""Validate and publish the exact Willow 0.4.3 browser export (standard library only)."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import unittest
import zipfile

ARCHIVE_NAME = "WillowsWonders_v0.4.3_Web_Playtest.zip"
ARCHIVE_SHA256 = "059c322df83cf7aa40579b2a9ab468c8e3b0d9edeae78c4bacbb6d996c14f4b8"
VERSION = "0.4.3"
PUBLISH = (
    "index.html", "index.js", "index.wasm", "index.pck", "index.png",
    "index.audio.worklet.js", "index.audio.position.worklet.js",
    "GODOT_THIRD_PARTY_NOTICES.txt",
)
ALLOWED = set(PUBLISH) | {"HOSTING_README.txt", "VALIDATION.txt"}
MAX_TOTAL = 70 * 1024 * 1024
MAX_ARCHIVE = 25 * 1024 * 1024
REDIRECT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow">
<meta http-equiv="refresh" content="0;url=./mobile/">
<title>Willow's Wonders - Mobile Playtest 0.4.3</title>
<style>body{font-family:system-ui;background:#fff6e3;color:#274f43;padding:2rem}a{color:inherit}</style>
</head><body><h1>Willow's Wonders</h1><p><a href="./mobile/">Open the iPhone and iPad playtest</a></p>
<p>Version 0.4.3. Early test build; progress is session-only.</p></body></html>
"""


def validate_entries(entries: list[zipfile.ZipInfo]) -> None:
    seen: set[str] = set()
    total = 0
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Unsafe archive path")
        if name not in ALLOWED or name in seen or entry.is_dir():
            raise ValueError("Unexpected or duplicate archive entry: " + name)
        if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise ValueError("Symlinks and encrypted entries are not permitted")
        total += entry.file_size
        if entry.file_size < 0 or total > MAX_TOTAL:
            raise ValueError("Archive exceeds the expected uncompressed size")
        seen.add(name)
    if not set(PUBLISH).issubset(seen):
        raise ValueError("Incomplete browser export; use the Web_Playtest ZIP, not Source")


def read_release(archive: Path) -> dict[str, bytes]:
    if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE:
        raise ValueError("Missing or oversized release archive")
    raw = archive.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("Release checksum mismatch. Upload the original 0.4.3 Web_Playtest ZIP unchanged.")
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        validate_entries(package.infolist())
        data = {name: package.read(name) for name in PUBLISH}
    if not data["index.wasm"].startswith(b"\x00asm\x01\x00\x00\x00"):
        raise ValueError("Invalid WebAssembly module")
    if not data["index.pck"].startswith(b"GDPC"):
        raise ValueError("Invalid Godot pack")
    if VERSION.encode() not in data["index.html"] or b"index.js" not in data["index.html"]:
        raise ValueError("Incorrect game launcher")
    return data


def publish(archive: Path, repository: Path, validate_only: bool = False) -> None:
    data = read_release(archive)
    print("Validated exact 0.4.3 export: %d playable files, %d bytes" % (len(data), sum(map(len, data.values()))))
    if validate_only:
        return
    destination = repository / "mobile"
    destination.mkdir(parents=True, exist_ok=True)
    # Validate the whole release before touching files. Git then publishes one atomic commit.
    for name, content in data.items():
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as tmp:
            tmp.write(content)
            temp_name = tmp.name
        os.replace(temp_name, destination / name)
    manifest = {
        "version": VERSION,
        "release_archive_sha256": ARCHIVE_SHA256,
        "source_commit": os.getenv("GITHUB_SHA", "local-validation"),
        "files": {name: {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                  for name, content in data.items()},
        "physical_iphone_ipad_test": "pending",
        "progress": "session-only",
        "teacher_voice_recordings_included": False,
    }
    (destination / "build.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (repository / "index.html").write_text(REDIRECT, encoding="utf-8")
    (repository / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    print("Prepared /mobile/ and root redirect. This script does not claim Safari testing.")


class PublisherTests(unittest.TestCase):
    def entries(self) -> list[zipfile.ZipInfo]:
        return [zipfile.ZipInfo(name) for name in PUBLISH]

    def test_complete_export(self):
        validate_entries(self.entries())

    def test_metadata_allowed(self):
        validate_entries(self.entries() + [zipfile.ZipInfo("VALIDATION.txt")])

    def test_missing_wasm_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries([x for x in self.entries() if x.filename != "index.wasm"])

    def test_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries(self.entries() + [zipfile.ZipInfo("index.html")])

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries(self.entries() + [zipfile.ZipInfo("../index.html")])

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries(self.entries() + [zipfile.ZipInfo("/index.html")])

    def test_windows_path_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries(self.entries() + [zipfile.ZipInfo("a\\index.html")])

    def test_unexpected_file_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries(self.entries() + [zipfile.ZipInfo("private_recording.wav")])

    def test_symlink_rejected(self):
        entries = self.entries()
        entries[0].external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            validate_entries(entries)

    def test_encrypted_rejected(self):
        entries = self.entries()
        entries[0].flag_bits |= 1
        with self.assertRaises(ValueError):
            validate_entries(entries)

    def test_size_limit(self):
        entries = self.entries()
        entries[0].file_size = MAX_TOTAL + 1
        with self.assertRaises(ValueError):
            validate_entries(entries)

    def test_invalid_archive_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / ARCHIVE_NAME
            archive.write_bytes(b"not the approved release")
            with self.assertRaises(ValueError):
                publish(archive, root)
            self.assertFalse((root / "mobile").exists())
            self.assertFalse((root / "index.html").exists())

    def test_subfolder_archive_rejected(self):
        with self.assertRaises(ValueError):
            validate_entries([zipfile.ZipInfo("export/" + name) for name in PUBLISH])

    def test_missing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                read_release(Path(tmp) / "absent.zip")

    def test_export_filenames_unchanged(self):
        self.assertEqual(len(PUBLISH), 8)
        self.assertIn("index.audio.worklet.js", PUBLISH)
        self.assertIn("GODOT_THIRD_PARTY_NOTICES.txt", PUBLISH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--archive", type=Path, default=Path(ARCHIVE_NAME))
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PublisherTests))
        raise SystemExit(0 if result.wasSuccessful() else 1)
    publish(args.archive, args.repository, args.validate_only)
