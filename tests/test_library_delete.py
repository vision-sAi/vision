import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vision import library


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.patch = mock.patch.object(library, "_session_logs", return_value=[])
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_deletes_upload_and_video_digest(self):
        clip = self.dir / "20260925-120000.mov"
        clip.write_bytes(b"x")
        clip.with_suffix(".json").write_text("{}")
        frames = self.dir / "20260925-120000-frames"
        frames.mkdir()
        (frames / "f1.jpg").write_bytes(b"x")
        self.assertTrue(library.delete(str(clip), self.dir, lambda p: True))
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_refuses_paths_the_library_does_not_list(self):
        outside = Path(tempfile.mkdtemp()) / "keep.png"
        outside.write_bytes(b"x")
        self.assertFalse(library.delete(str(outside), self.dir, lambda p: True))
        self.assertTrue(outside.exists())

    def test_delete_many_lists_once_and_skips_unknown(self):
        a, b = self.dir / "a.png", self.dir / "b.png"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        outside = Path(tempfile.mkdtemp()) / "keep.png"
        outside.write_bytes(b"x")
        with mock.patch.object(library, "library", wraps=library.library) as listing:
            done = library.delete_many([str(a), str(outside), str(b), str(a)], self.dir, lambda p: True)
        self.assertEqual(listing.call_count, 1)
        self.assertEqual(done, [str(a), str(b)])
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertTrue(outside.exists())


class ProjectLibraryTests(unittest.TestCase):
    """A project's Library: the index of what was sent in it and what its threads produced."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.folder = self.dir / "project"

    def test_index_list_relocate_and_unindex(self):
        made = self.dir / "wt" / "docs" / "plan.md"
        made.parent.mkdir(parents=True)
        made.write_text("plan")
        sent = self.dir / "20260925-113458-e5d17a-brief.pdf"
        sent.write_bytes(b"pdf")
        library.index_thread_result(self.folder, {"id": "t1", "branch": "vision/p/x"},
                                    {"artifacts": [str(made), str(self.dir / "missing.txt")], "summary": "", "findings": []})
        library.index_project(self.folder, [{"path": str(sent), "source": "you"}])
        library.index_project(self.folder, [{"path": str(sent), "source": "you"}])  # once, however often it's sent
        entries = library.project_entries(self.folder)
        self.assertEqual([e["name"] for e in entries], ["plan.md", sent.name])
        self.assertEqual(entries[0]["branch"], "vision/p/x")
        shown = {i["name"]: i for i in library.project_library(self.folder, lambda p: True)}
        self.assertEqual(set(shown), {"plan.md", "brief.pdf"})  # an upload's stamp comes off its name
        self.assertEqual(shown["brief.pdf"]["source"], "you")
        # Landed: the file is in the project's folder now, and the index follows it there.
        landed = self.dir / "main" / "docs" / "plan.md"
        landed.parent.mkdir(parents=True)
        landed.write_text("plan")
        library.relocate_project(self.folder, str(self.dir / "wt"), str(self.dir / "main"))
        self.assertEqual(library.project_entries(self.folder)[0]["path"], str(landed))
        self.assertEqual(library.project_entries(self.folder)[0]["branch"], "")
        gone = library.unindex_project(self.folder, str(landed))
        self.assertEqual(gone["source"], "thread")
        self.assertTrue(landed.exists())  # only the entry goes: the file belongs to the repo
        self.assertIsNone(library.unindex_project(self.folder, str(landed)))

    def test_the_chats_reply_pictures_join_in(self):
        pic = self.dir / "chart.png"
        pic.write_bytes(b"png")
        log = self.dir / "session.jsonl"
        log.write_text('{"text": "here ![the chart](' + str(pic) + ')"}\n')
        items = library.project_library(self.folder, lambda p: True, [str(log)])
        self.assertEqual([(i["name"], i["source"], i["caption"]) for i in items], [("chart.png", "vision", "the chart")])
        self.assertEqual(library.project_library(self.folder, lambda p: False, [str(log)]), [])

    def test_an_artifact_entry_splits_off_its_caption_unless_the_file_name_holds_the_dash(self):
        pic = self.dir / "a — b.png"
        pic.write_bytes(b"png")
        shot = self.dir / "shot.png"
        self.assertEqual(library.artifact_entry(f"{shot} — Login, dark"), {"path": str(shot), "caption": "Login, dark"})
        self.assertEqual(library.artifact_entry(str(shot)), {"path": str(shot), "caption": ""})
        self.assertEqual(library.artifact_entry(str(pic)), {"path": str(pic), "caption": ""})


if __name__ == "__main__":
    unittest.main()
