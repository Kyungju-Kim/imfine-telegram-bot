"""
user_store.py
Telegram Chat ID ↔ Notion User ID/Name 매핑을 JSON 파일로 저장/불러오기
Railway 환경에서는 재배포 시 파일이 초기화될 수 있으므로
소규모 팀용으로는 충분하지만, 영구 보존이 필요하면 DB로 교체 권장
"""

import json
import os

STORE_PATH = "users.json"


def _load() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict):
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def register_user(telegram_id: int, notion_user_id: str, notion_name: str):
    """팀원 등록 또는 업데이트"""
    data = _load()
    data[str(telegram_id)] = {
        "notion_user_id": notion_user_id,
        "notion_name": notion_name,
    }
    _save(data)


def get_user(telegram_id: int) -> dict | None:
    """등록된 유저 정보 반환. 없으면 None"""
    data = _load()
    return data.get(str(telegram_id))


def remove_user(telegram_id: int):
    """등록 해제"""
    data = _load()
    data.pop(str(telegram_id), None)
    _save(data)


def list_users() -> dict:
    return _load()
