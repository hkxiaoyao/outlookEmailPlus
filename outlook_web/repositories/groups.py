from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional

from outlook_web.db import get_db


def load_groups() -> List[Dict]:
    """加载所有分组（临时邮箱分组排在最前面）"""
    db = get_db()
    cursor = db.execute("""
        SELECT * FROM groups
        ORDER BY
            CASE WHEN name = '临时邮箱' THEN 0 ELSE 1 END,
            id
    """)
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def get_group_by_id(group_id: int) -> Optional[Dict]:
    """根据 ID 获取分组"""
    db = get_db()
    cursor = db.execute("SELECT * FROM groups WHERE id = ?", (group_id,))
    row = cursor.fetchone()
    return dict(row) if row else None


def add_group(
    name: str,
    description: str = "",
    color: str = "#1a1a1a",
    proxy_url: str = "",
) -> Optional[int]:
    """添加分组"""
    db = get_db()
    try:
        cursor = db.execute(
            """
            INSERT INTO groups (name, description, color, proxy_url)
            VALUES (?, ?, ?, ?)
        """,
            (name, description, color, proxy_url or ""),
        )
        db.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None


def update_group(
    group_id: int,
    name: str,
    description: str,
    color: str,
    proxy_url: str = "",
) -> bool:
    """更新分组"""
    db = get_db()
    try:
        db.execute(
            """
            UPDATE groups SET name = ?, description = ?, color = ?, proxy_url = ?
            WHERE id = ?
        """,
            (name, description, color, proxy_url or "", group_id),
        )
        db.commit()
        return True
    except Exception:
        return False


def get_default_group_id() -> int:
    """获取默认分组 ID（不依赖固定 id=1，增强兼容性）"""
    db = get_db()
    try:
        row = db.execute("SELECT id FROM groups WHERE name = '默认分组' LIMIT 1").fetchone()
        return row["id"] if row else 1
    except Exception:
        return 1


def delete_group(group_id: int) -> bool:
    """删除分组（将该分组下的邮箱移到默认分组）"""
    db = get_db()
    try:
        row = db.execute("SELECT id, name, is_system FROM groups WHERE id = ?", (group_id,)).fetchone()
        if not row:
            return False
        if row["is_system"]:
            return False

        default_group_id = get_default_group_id()
        if group_id == default_group_id or row["name"] == "默认分组":
            return False

        db.execute(
            "UPDATE accounts SET group_id = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
            (default_group_id, group_id),
        )
        db.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        db.commit()
        return True
    except Exception:
        return False


def get_group_account_count(group_id: int) -> int:
    """获取分组下的邮箱数量"""
    db = get_db()
    cursor = db.execute("SELECT COUNT(*) as count FROM accounts WHERE group_id = ?", (group_id,))
    row = cursor.fetchone()
    return row["count"] if row else 0


def get_group_by_name(name: str) -> Optional[Dict]:
    """按名称查找分组（精确匹配，不区分大小写）"""
    db = get_db()
    cursor = db.execute("SELECT * FROM groups WHERE LOWER(name) = LOWER(?)", (name,))
    row = cursor.fetchone()
    return dict(row) if row else None
