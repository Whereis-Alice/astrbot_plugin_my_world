"""README / 文档里的相对链接必须真的存在。

只检查仓库内的相对路径（含带 #锚点 的写法）；外部网址、页内锚点和绝对路径不检查，
免得文档一改外部链接就红。第一次打开插件的用户基本只看 README 和文档目录，
一条 404 的文档链接就够劝退了，所以把它钉在测试里。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _doc_files() -> list[Path]:
    files = [ROOT / "README.md", ROOT / "NOTICE.md", ROOT / "CHANGELOG.md"]
    files.extend(sorted((ROOT / "docs").glob("*.md")))
    return [path for path in files if path.is_file()]


class DocsLinkTests(unittest.TestCase):
    def test_relative_links_resolve(self) -> None:
        missing: list[str] = []
        for path in _doc_files():
            text = path.read_text(encoding="utf-8")
            for raw in _LINK.findall(text):
                target = raw.strip()
                if not target or target.startswith(
                    ("http://", "https://", "mailto:", "#", "/")
                ):
                    continue
                target = target.split("#", 1)[0].strip()
                if not target:
                    continue
                if not (path.parent / target).resolve().exists():
                    missing.append(f"{path.relative_to(ROOT)} -> {raw}")
        self.assertEqual(missing, [], "这些相对链接指向不存在的文件：\n" + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
