"""
用户分组管理器
- 支持用户分组（如"蔚来黑子"分组包含：某某, 某某某 …）
- 支持从 .txt 文件导入用户（user_id nickname 格式）
- 自动记录最近使用的用户组合
- 数据存储于项目根目录 user_groups.json
"""

import json
from pathlib import Path
from dataclasses import dataclass, field
from datetime import date

STORAGE_FILE = Path(__file__).parent.parent / "user_groups.json"


@dataclass
class UserGroup:
    name: str
    users: list = field(default_factory=list)  # [{"user_id": "xxx", "nickname": "xxx"}, ...]
    created_at: str = ""  # ISO date
    note: str = ""


class UserManager:
    """用户分组管理器（单例）"""

    def __init__(self):
        self._groups: list = []
        self._recent: list = []  # 最近使用的用户组合（紧凑描述字符串）
        self._load()

    # ── 持久化 ──

    def _load(self):
        """从 JSON 文件加载"""
        if STORAGE_FILE.exists():
            try:
                data = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
                self._groups = [
                    UserGroup(
                        name=g.get("name", ""),
                        users=g.get("users", []),
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
                    "users": g.users,
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

    def get_group(self, name: str) -> UserGroup or None:
        """按名称获取分组"""
        for g in self._groups:
            if g.name == name:
                return g
        return None

    def add_group(self, name: str, users: list, note: str = "") -> bool:
        """
        添加分组。同名则合并用户（按 user_id 去重）。
        返回 True=新增, False=更新
        """
        existing = self.get_group(name)
        if existing:
            # 更新：合并用户（按 user_id 去重）
            existing_ids = {u.get("user_id", "") for u in existing.users}
            new_users = [u for u in users if u.get("user_id", "") not in existing_ids]
            existing.users.extend(new_users)
            if note:
                existing.note = note
            self._save()
            return False
        else:
            self._groups.append(
                UserGroup(
                    name=name,
                    users=list(users),
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

    def update_group_users(self, name: str, users: list):
        """更新分组的全部用户列表"""
        g = self.get_group(name)
        if g:
            g.users = list(users)
            self._save()

    # ── 近期用户 ──

    @property
    def recent(self) -> list:
        return self._recent

    def add_recent(self, user_display: str):
        """记录最近使用的用户组合（去重，最多保留20条）"""
        if not user_display or not user_display.strip():
            return
        ds = user_display.strip()
        if ds in self._recent:
            self._recent.remove(ds)
        self._recent.insert(0, ds)
        if len(self._recent) > 20:
            self._recent = self._recent[:20]
        self._save()

    # ── 导入 ──

    @staticmethod
    def import_from_file(filepath: str) -> list:
        """
        从文本文件导入用户列表。
        支持格式：
            user_id nickname    （空格分隔）
            user_id              （仅 ID，昵称=ID）
            # 开头行为注释，空行跳过
        返回用户列表 [{"user_id": "xxx", "nickname": "xxx"}, ...]
        """
        text = Path(filepath).read_text(encoding="utf-8")
        return UserManager._parse_user_text(text)

    @staticmethod
    def _parse_user_text(text: str) -> list:
        """从文本内容解析用户列表（每行 user_id nickname 或仅 user_id）"""
        users = []
        seen = set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 1)
            if parts:
                uid = parts[0]
                if uid in seen:
                    continue
                seen.add(uid)
                nickname = parts[1].strip() if len(parts) > 1 else uid
                users.append({"user_id": uid, "nickname": nickname})
        return users


# 全局单例
user_mgr = UserManager()
