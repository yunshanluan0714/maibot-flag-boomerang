"""可选宿主补丁：为 MaiBot 插件运行时注册 memory.ingest_text 长期记忆写入能力（幂等，适配 1.4.x）。

用法：
    python patches/apply_memory_ingest_cap_patch.py --maibot-root /path/to/maibot
    python patches/apply_memory_ingest_cap_patch.py --maibot-root /path/to/maibot --check
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import py_compile
import tempfile

MARKER = "[patch:memory-ingest-cap-v1]"

OLD_DATA = """    async def _cap_knowledge_search(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:"""

NEW_DATA = '''    async def _cap_memory_ingest_text(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """[patch:memory-ingest-cap-v1] 将插件结构化高价值事件写入长期记忆与向量库。"""
        del capability
        text = str(args.get("text") or "").strip()
        if not text:
            return {"success": False, "error": "缺少必要参数 text"}
        external_id = str(args.get("external_id") or "").strip()
        source_type = str(args.get("source_type") or f"plugin:{plugin_id}").strip() or f"plugin:{plugin_id}"
        chat_id = str(args.get("chat_id") or "").strip()
        user_id = str(args.get("user_id") or "").strip()
        group_id = str(args.get("group_id") or "").strip()
        respect_filter = bool(args.get("respect_filter", True))
        participants = [str(x).strip() for x in (args.get("participants") or []) if str(x).strip()]
        tags = [str(x).strip() for x in (args.get("tags") or []) if str(x).strip()]
        metadata = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
        metadata = {**metadata, "plugin_id": plugin_id}
        timestamp = args.get("timestamp")

        try:
            from src.services.memory_service import memory_service

            result = await memory_service.ingest_text(
                external_id=external_id,
                source_type=source_type,
                text=text,
                chat_id=chat_id,
                participants=participants,
                timestamp=float(timestamp) if timestamp is not None else None,
                tags=tags,
                metadata=metadata,
                respect_filter=respect_filter,
                user_id=user_id,
                group_id=group_id,
            )
            return {
                "success": bool(result.success),
                "stored_ids": list(result.stored_ids),
                "skipped_ids": list(result.skipped_ids),
                "detail": str(result.detail or ""),
            }
        except Exception as e:
            logger.error(f"[cap.memory.ingest_text] 执行失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    async def _cap_knowledge_search(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:'''

OLD_REG = '    _register("knowledge.search", manager._cap_knowledge_search)'
NEW_REG = (
    '    _register("knowledge.search", manager._cap_knowledge_search)\n'
    '    _register("memory.ingest_text", manager._cap_memory_ingest_text)  # [patch:memory-ingest-cap-v1]'
)


def resolve_targets(maibot_root: Path) -> tuple[Path, Path]:
    base = maibot_root / "src" / "plugin_runtime" / "capabilities"
    return base / "data.py", base / "registry.py"


def check_patched(maibot_root: Path) -> bool:
    data_py, reg_py = resolve_targets(maibot_root)
    if not data_py.is_file() or not reg_py.is_file():
        return False
    return MARKER in data_py.read_text(encoding="utf-8") and MARKER in reg_py.read_text(encoding="utf-8")


def patch_file(path: Path, old: str, new: str) -> bool:
    if not path.is_file():
        raise FileNotFoundError(f"未找到目标文件: {path}")
    src = path.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"{path.name}: 补丁已存在，跳过")
        return False
    if src.count(old) != 1:
        raise RuntimeError(f"{path.name} 锚点匹配异常 (count={src.count(old)}): {old[:60]!r}")
    patched = src.replace(old, new, 1)
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf-8", delete=False) as tf:
        tf.write(patched)
        tmp_path = Path(tf.name)
    try:
        py_compile.compile(str(tmp_path), cfile=str(tmp_path) + "c", doraise=True)
    finally:
        tmp_path.unlink(missing_ok=True)
        Path(str(tmp_path) + "c").unlink(missing_ok=True)
    path.write_text(patched, encoding="utf-8")
    print(f"{path.name}: 已写入 memory.ingest_text 长期记忆写入补丁")
    return True


def apply_patch(maibot_root: Path) -> bool:
    data_py, reg_py = resolve_targets(maibot_root)
    c1 = patch_file(data_py, OLD_DATA, NEW_DATA)
    c2 = patch_file(reg_py, OLD_REG, NEW_REG)
    return c1 or c2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="为 MaiBot 插件运行时注册 memory.ingest_text 长期记忆写入能力")
    parser.add_argument(
        "--maibot-root",
        default=os.environ.get("MAIBOT_ROOT", "."),
        help="MaiBot 源码根目录路径（包含 src/plugin_runtime/capabilities/），也可通过 MAIBOT_ROOT 环境变量指定",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="仅检查补丁是否已安装（已安装返回 0，未安装返回 1）",
    )
    args = parser.parse_args(argv)
    root = Path(args.maibot_root).resolve()

    if args.check:
        ok = check_patched(root)
        print("memory.ingest_text 补丁状态: OK" if ok else "memory.ingest_text 补丁状态: NOT_PATCHED")
        return 0 if ok else 1

    apply_patch(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
