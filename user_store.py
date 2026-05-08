"""
user_store.py
- users.json: 로컬 캐시 (빠른 읽기용)
- Google Sheets: 영구 백업 (재배포 후 복구용)

봇 시작 시 Sheets → users.json 복구
등록/해제 시 둘 다 동기화
"""

import json
import os
import logging

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

STORE_PATH = "users.json"
SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


# ─── Google Sheets 클라이언트 ─────────────────────────────────────────

def _get_sheet():
    if not GOOGLE_CREDENTIALS or not SHEETS_ID:
        return None
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEETS_ID).sheet1
        return sheet
    except Exception as e:
        logger.error(f"[Sheets] 연결 실패: {e}")
        return None


# ─── 로컬 JSON ───────────────────────────────────────────────────────

def _load() -> dict:
    if not os.path.exists(STORE_PATH):
        return {}
    with open(STORE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict):
    with open(STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── Sheets 동기화 ───────────────────────────────────────────────────

def _sheets_save_all(data: dict):
    """로컬 데이터 전체를 Sheets에 덮어쓰기"""
    sheet = _get_sheet()
    if not sheet:
        return
    try:
        sheet.clear()
        sheet.append_row(["telegram_id", "notion_user_id", "notion_name"])
        for telegram_id, info in data.items():
            sheet.append_row([
                telegram_id,
                info["notion_user_id"],
                info["notion_name"]
            ])
        logger.info(f"[Sheets] 전체 저장 완료 ({len(data)}명)")
    except Exception as e:
        logger.error(f"[Sheets] 저장 실패: {e}")


def _sheets_delete_row(telegram_id: str):
    """Sheets에서 특정 유저 행 삭제"""
    sheet = _get_sheet()
    if not sheet:
        return
    try:
        cell = sheet.find(str(telegram_id))
        if cell:
            sheet.delete_rows(cell.row)
            logger.info(f"[Sheets] {telegram_id} 삭제 완료")
    except Exception as e:
        logger.error(f"[Sheets] 삭제 실패: {e}")


# ─── 시작 시 복구 ────────────────────────────────────────────────────

def restore_from_sheets():
    """봇 시작 시 Sheets → users.json 복구"""
    sheet = _get_sheet()
    if not sheet:
        logger.warning("[Sheets] 연결 없음 - 복구 스킵")
        return

    try:
        rows = sheet.get_all_records()
        if not rows:
            logger.info("[Sheets] 저장된 유저 없음")
            return

        data = {}
        for row in rows:
            tid = str(row.get("telegram_id", "")).strip()
            nid = str(row.get("notion_user_id", "")).strip()
            name = str(row.get("notion_name", "")).strip()
            if tid and nid and name:
                data[tid] = {
                    "notion_user_id": nid,
                    "notion_name": name
                }

        _save(data)
        logger.info(f"[Sheets] 복구 완료 ({len(data)}명): {[v['notion_name'] for v in data.values()]}")
    except Exception as e:
        logger.error(f"[Sheets] 복구 실패: {e}")


# ─── 공개 API ────────────────────────────────────────────────────────

def register_user(telegram_id: int, notion_user_id: str, notion_name: str):
    data = _load()
    data[str(telegram_id)] = {
        "notion_user_id": notion_user_id,
        "notion_name": notion_name,
    }
    _save(data)
    _sheets_save_all(data)


def get_user(telegram_id: int) -> dict | None:
    data = _load()
    return data.get(str(telegram_id))


def remove_user(telegram_id: int):
    data = _load()
    data.pop(str(telegram_id), None)
    _save(data)
    _sheets_delete_row(str(telegram_id))


def list_users() -> dict:
    return _load()
