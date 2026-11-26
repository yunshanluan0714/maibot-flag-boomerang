"""Unit tests for the optional memory.ingest_text host capability patch."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent / "patches"))
from apply_memory_ingest_cap_patch import MARKER, apply_patch, check_patched


class MemoryIngestPatchTests(unittest.TestCase):
    def test_apply_and_idempotent_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cap_dir = root / "src" / "plugin_runtime" / "capabilities"
            cap_dir.mkdir(parents=True)
            data_py = cap_dir / "data.py"
            reg_py = cap_dir / "registry.py"

            data_py.write_text(
                "from typing import Any, Dict\n\n"
                "class Demo:\n"
                "    async def _cap_knowledge_search(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:\n"
                "        return {}\n",
                encoding="utf-8",
            )
            reg_py.write_text(
                "def register_all(manager, _register):\n"
                '    _register("knowledge.search", manager._cap_knowledge_search)\n',
                encoding="utf-8",
            )

            self.assertFalse(check_patched(root))
            changed_first = apply_patch(root)
            self.assertTrue(changed_first)
            self.assertTrue(check_patched(root))
            self.assertIn(MARKER, data_py.read_text(encoding="utf-8"))
            self.assertIn("memory.ingest_text", reg_py.read_text(encoding="utf-8"))

            changed_second = apply_patch(root)
            self.assertFalse(changed_second)
            self.assertTrue(check_patched(root))


if __name__ == "__main__":
    unittest.main()
