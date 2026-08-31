import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from vaultsync import deployment as DEP


def vault(d):
    # matching: folder name == index label
    Path(d, "Operation Sentinel").mkdir()
    Path(d, "Operation Sentinel", "00 START HERE.md").write_text(
        "---\ntype: deployment-index\noperation: Operation Sentinel\n---\n")
    # mismatching: folder renamed, label kept (the case that used to split the product silently)
    Path(d, "BQ26").mkdir()
    Path(d, "BQ26", "00 START HERE.md").write_text(
        "---\ntype: deployment-index\noperation: Exercise Bold Quest\n---\n")
    # informal: no index note at all
    Path(d, "Scratch Op").mkdir()
    Path(d, "Scratch Op", "note.md").write_text("# a note\n")
    # noise that must not appear
    Path(d, ".obsidian").mkdir()
    Path(d, "loose note.md").write_text("# loose\n")


class TestDeployment(unittest.TestCase):
    def test_resolve_all_three_shapes(self):
        with tempfile.TemporaryDirectory() as d:
            vault(d)
            deps = {x.folder: x for x in DEP.resolve(d)}
            self.assertEqual(set(deps), {"Operation Sentinel", "BQ26", "Scratch Op"})
            self.assertEqual(deps["Operation Sentinel"].label, "Operation Sentinel")
            self.assertFalse(deps["Operation Sentinel"].mismatch)
            self.assertEqual(deps["BQ26"].label, "Exercise Bold Quest")
            self.assertTrue(deps["BQ26"].mismatch)
            self.assertEqual(deps["Scratch Op"].label, "Scratch Op")
            self.assertFalse(deps["Scratch Op"].has_index)

    def test_folder_for_label_and_folder_name(self):
        with tempfile.TemporaryDirectory() as d:
            vault(d)
            self.assertEqual(DEP.folder_for(d, "Exercise Bold Quest"), "BQ26")
            self.assertEqual(DEP.folder_for(d, "BQ26"), "BQ26")           # folder accepted directly
            self.assertEqual(DEP.folder_for(d, "Operation Sentinel"), "Operation Sentinel")
            self.assertIsNone(DEP.folder_for(d, "No Such Op"))
            self.assertIsNone(DEP.folder_for(d, ""))

    def test_label_for(self):
        with tempfile.TemporaryDirectory() as d:
            vault(d)
            self.assertEqual(DEP.label_for(d, "BQ26"), "Exercise Bold Quest")
            self.assertEqual(DEP.label_for(d, "Scratch Op"), "Scratch Op")
            self.assertEqual(DEP.label_for(d, "unknown"), "unknown")

    def test_mismatches_reported(self):
        with tempfile.TemporaryDirectory() as d:
            vault(d)
            mm = DEP.mismatches(d)
            self.assertEqual([x.folder for x in mm], ["BQ26"])


if __name__ == "__main__":
    unittest.main()
