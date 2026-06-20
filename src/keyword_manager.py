"""
关键词分组管理器
- 支持关键词分组（如"蔚来"分组包含：换电、蔚来、NIO…）
- 支持从 .txt 文件导入关键词（一行一个，或任意常见分隔符）
- 自动记录最近使用的关键词
- 数据存储于项目根目录 keyword_groups.json
"""

import json, re
from pathlib import Path
from dataclasses import dataclass, field

STORAGE_FILE = Path(__file__).parent.parent / "keyword_groups.json"


@dataclass
class KeywordGroup:
    name: str
    keywords: list = field(default_factory=list)
    created_at: str = ""  # ISO date
    note: str = ""


class KeywordManager:
    """关键词分组管理器（单例）"""

    def __init__(self):
        self._groups: list = []
        self._recent: list = []  # 最近使用的关键词（非分组）
        self._load()

    # ── 持久化 ──

    def _load(self):
        """从 JSON 文件加载"""
        if STORAGE_FILE.exists():
            try:
                data = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
                self._groups = [
                    KeywordGroup(
                        name=g.get("name", ""),
                        keywords=g.get("keywords", []),
                        created_at=g.get("created_at", ""),
                        note=g.get("note", ""),
                    )
                    for g in data.get("groups", [])
                ]
                self._recent = data.get("recent", [])
            except Exception:
                self._groups = []
                self._recent = []

    def _save(self):
        """保存到 JSON 文件"""
        data = {
            "groups": [
                {
                    "name": g.name,
                    "keywords": g.keywords,
                    "created_at": g.created_at,
                    "note": g.note,
                }
                for g in self._groups
            ],
            "recent": self._recent,
        }
        STORAGE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 分组操作 ──

    @property
    def groups(self) -> list:
        """返回所有分组"""
        return self._groups

    def group_names(self) -> list:
        """返回所有分组名称列表"""
        return [g.name for g in self._groups]

    def get_group(self, name: str) -> KeywordGroup or None:
        """按名称获取分组"""
        for g in self._groups:
            if g.name == name:
                return g
        return None

    def add_group(self, name: str, keywords: list, note: str = "", replace: bool = False) -> bool:
        """
        添加分组。同名则更新关键词。
        默认合并去重；replace=True 则完全替换。
        返回 True=新增, False=更新
        """
        from datetime import date
        existing = self.get_group(name)
        if existing:
            # 更新：合并或替换
            if replace:
                existing.keywords = list(dict.fromkeys(keywords))
            else:
                combined = list(dict.fromkeys(existing.keywords + keywords))
                existing.keywords = combined
            if note:
                existing.note = note
            self._save()
            return False
        else:
            self._groups.append(
                KeywordGroup(
                    name=name,
                    keywords=list(dict.fromkeys(keywords)),
                    created_at=str(date.today()),
                    note=note,
                )
            )
            self._save()
            return True

    def delete_group(self, name: str) -> bool:
        """删除分组。返回是否成功"""
        before = len(self._groups)
        self._groups = [g for g in self._groups if g.name != name]
        if len(self._groups) < before:
            self._save()
            return True
        return False

    def update_group_keywords(self, name: str, keywords: list):
        """更新分组的全部关键词"""
        g = self.get_group(name)
        if g:
            g.keywords = list(dict.fromkeys(keywords))
            self._save()

    # ── 近期关键词 ──

    @property
    def recent(self) -> list:
        return self._recent

    def add_recent(self, keyword_str: str):
        """记录最近使用的关键词（去重，最多保留20条）"""
        if not keyword_str or not keyword_str.strip():
            return
        kw = keyword_str.strip()
        if kw in self._recent:
            self._recent.remove(kw)
        self._recent.insert(0, kw)
        if len(self._recent) > 20:
            self._recent = self._recent[:20]
        self._save()

    # ── 导入 ──

    @staticmethod
    def import_from_file(filepath: str) -> list:
        """
        从文本文件导入关键词。
        支持格式：一行一个，或任意常见分隔符（逗号/顿号/分号/空格/Tab等）。
        #开头行为注释，空行跳过。
        返回关键词列表（去重去空白）。
        """
        text = Path(filepath).read_text(encoding="utf-8")
        keywords = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 用统一正则拆分：逗号（中英文）、顿号、分号（中英文）、空白字符（空格/Tab等）
            parts = re.split(r'[,，、;；\s]+', line)
            for p in parts:
                k = p.strip()
                if k and k not in keywords:
                    keywords.append(k)
        return keywords


# 全局单例
keyword_mgr = KeywordManager()
