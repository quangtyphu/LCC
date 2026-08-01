# -*- coding: utf-8 -*-
"""
CMS API — CRUD tài khoản XOSO66 + provision đăng ký.

Chạy:
  pip install fastapi uvicorn
  python xoso66_api.py
  # hoặc: uvicorn xoso66_api:app --host 127.0.0.1 --port 8799

Header: X-API-Key: <api_key trong xoso66_config.json>
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))
from xoso66_paths import apply_default_env

apply_default_env()

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from xoso66_accounts_db import (
    CMS_COLUMNS,
    create_account,
    delete_account,
    get_account,
    init_db,
    list_accounts,
    public_row,
    update_account,
)
from xoso66_account_save import DEFAULT_FUND_PASSWORD, save_account_with_site_sync

DEFAULT_LOGIN_PASSWORD = (os.environ.get("XOSO66_DEFAULT_LOGIN_PASSWORD") or "Valentine1").strip()
from xoso66_bulk_provision import parse_bulk_text, provision_bulk
from xoso66_provision import provision_account
from xoso66_config_util import configure_stdio_utf8

configure_stdio_utf8()

_DIR = Path(__file__).resolve().parent
from xoso66_paths import default_config_path

_CONFIG = Path(os.environ.get("XOSO66_CONFIG") or str(default_config_path()))
_WEB_DIR = _DIR / "web"


def _normalize_qr_base64_payload(raw: Any) -> str:
    """Chuẩn payload base64 thuần (không prefix data:) cho modal."""
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    if s.startswith("data:") and "," in s:
        return s.split(",", 1)[1]
    return s.lstrip()


def _enrich_deposit_for_ui(out: dict[str, Any]) -> dict[str, Any]:
    """Thêm URL ảnh QR + object display cho modal (giống LC79)."""
    from xoso66_deposit import qr_image_to_data_uri

    aid = str(out.get("account_id") or "")
    row = get_account(aid) if aid else None
    ti = out.get("transfer_info") if isinstance(out.get("transfer_info"), dict) else {}

    b64 = _normalize_qr_base64_payload(out.get("qr_base64"))
    if not b64:
        b64 = _normalize_qr_base64_payload(ti.get("qr_base64"))
    uri = ti.get("qr_data_uri")
    if not b64 and isinstance(uri, str) and uri.startswith("data:") and "," in uri:
        b64 = uri.split(",", 1)[1]
    if not b64:
        data_uri = qr_image_to_data_uri(
            qr_path=str(out.get("qr_image_path") or ""),
            transfer_info=ti,
        )
        if isinstance(data_uri, str) and "," in data_uri:
            b64 = data_uri.split(",", 1)[1]
    if b64:
        out["qr_base64"] = b64

    qr_image_url = ""
    qr_path = out.get("qr_image_path")
    if qr_path:
        p = Path(str(qr_path))
        if p.is_file():
            qr_image_url = "/qr_outputs/" + p.name.replace("\\", "/")
    out["qr_image_url"] = qr_image_url

    ok = bool(out.get("ok"))
    out["message"] = (
        "Tạo lệnh nạp tiền thành công"
        if ok
        else f"Tạo lệnh nạp tiền thất bại: {out.get('error') or 'unknown'}"
    )
    out["data"] = {
        "username": (row or {}).get("username") or aid,
        "name": ti.get("account_name") or "",
        "accountHolder": ti.get("account_name") or "",
        "receiver": ti.get("account_no") or "",
        "accountNumber": ti.get("account_no") or "",
        "amount": out.get("amount") or ti.get("amount"),
        "msg": ti.get("transfer_content") or "",
        "transferContent": ti.get("transfer_content") or "",
        "qr_link": qr_image_url,
        "qr_base64": out.get("qr_base64") or "",
        "expired": ti.get("expire_seconds") or 300,
        "pay_url": out.get("pay_url"),
        "trade_no": out.get("trade_no"),
        # Debug để bạn test khi QR/STK/NDCK không parse được.
        "method": out.get("method") or "",
        "transfer_info_error": out.get("transfer_info_error") or "",
        "deposit_raw_error": out.get("error") or "",
    }
    return out


app = FastAPI(title="XOSO66 Accounts API", version="1.0")

# CMS QuanLyChrome (port 3000) gọi API trực tiếp — không cần proxy server.js
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://0.0.0.0:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _log_incoming_api_requests(request, call_next):
    """Luôn in POST/PUT tới /api/* — kể cả 401, để debug CMS không thấy log."""
    path = request.url.path or ""
    if path.startswith("/api/") and request.method in (
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ):
        client = request.client.host if request.client else "?"
        print(
            f"[API] ← {request.method} {path} (client={client})",
            flush=True,
        )
    return await call_next(request)


def _load_api_key() -> str:
    env = (os.environ.get("XOSO66_API_KEY") or "").strip()
    if env:
        return env
    try:
        from xoso66_config_util import load_config

        k = str(load_config().get("api_key") or "").strip()
        if k:
            return k
    except Exception:
        pass
    if _CONFIG.is_file():
        try:
            return str(json.loads(_CONFIG.read_text(encoding="utf-8")).get("api_key") or "").strip()
        except Exception:
            pass
    return ""


def require_api_key(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> None:
    expected = _load_api_key()
    if not expected:
        return
    got = (x_api_key or "").strip()
    if got == expected:
        return
    # CMS QuanLyChrome / proxy local hay quên gửi header — chỉ cho loopback.
    client = (request.client.host if request.client else "") or ""
    if not got and client in ("127.0.0.1", "::1"):
        return
    got_disp = f"{got[:4]}…{got[-2:]}" if len(got) >= 8 else (got or "(trống)")
    exp_disp = (
        f"{expected[:4]}…{expected[-2:]}" if len(expected) >= 8 else expected
    )
    print(
        f"[API] 401 X-API-Key không hợp lệ (got={got_disp}, expected={exp_disp}, client={client})",
        flush=True,
    )
    raise HTTPException(status_code=401, detail="API key không hợp lệ")


class AccountCreate(BaseModel):
    username: str
    password: str = ""
    phone: str = ""
    account_holder: str = ""
    fund_password: str = ""
    bank_code: str = ""
    bank_name: str = ""
    account_number: str = ""
    proxy: str = ""
    default_card_id: int | None = None
    device: str = ""
    total_deposit: float = 0
    total_withdraw: float = 0
    balance: float = 0
    status: str = "new"
    vip_level: str = ""
    vip_progress: int = 0
    daily_bet_total: float = 0
    daily_bet_day: str = ""


class AccountUpdate(BaseModel):
    username: str | None = None
    password: str | None = None
    phone: str | None = None
    account_holder: str | None = None
    fund_password: str | None = None
    bank_code: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    proxy: str | None = None
    default_card_id: int | None = None
    device: str | None = None
    total_deposit: float | None = None
    total_withdraw: float | None = None
    balance: float | None = None
    status: str | None = None
    vip_level: str | None = None
    vip_progress: int | None = None
    daily_bet_total: float | None = None
    daily_bet_day: str | None = None


class SyncChromeBody(BaseModel):
    device: str = ""
    force_login: bool = False
    timeout_sec: int = 30


class ProvisionBody(BaseModel):
    username: str
    password: str
    phone: str
    account_holder: str
    proxy: str = Field(..., description="Bắt buộc host:port:user:pass")
    fund_password: str | None = None
    account_number: str | None = None
    bank: str | None = None
    bank_code: str | None = None
    bank_name: str | None = None
    device: str | None = None
    vip_level: str | None = None
    step: str | None = Field(
        None,
        description="register | fund_password | bank_bind | all (mặc định all)",
    )
    account_id: str | None = Field(None, description="Bắt buộc từ bước 2 trở đi")


class BulkProvisionBody(BaseModel):
    rows: list[dict[str, Any]]
    defaults: dict[str, Any] | None = None
    stop_on_error: bool = False
    delay_seconds: float = Field(3.0, ge=0, le=120)


class DepositBody(BaseModel):
    account_id: str
    amount: int = Field(..., gt=0)
    wait: bool = Field(True, description="True: chờ xong rồi trả kết quả (UI)")


class WithdrawBody(BaseModel):
    account_id: str
    amount: int = Field(..., gt=0)
    fund_password: str | None = None
    use_browser: bool = False
    wait: bool = Field(True, description="True: chờ xong rồi trả kết quả (UI)")


class RefreshSessionBody(BaseModel):
    account_id: str
    force_login: bool = False


class CheckWithdrawBody(BaseModel):
    account_id: str = ""
    username: str = ""
    amount: int | None = None
    serial_no: str | None = None
    poll: bool = False
    poll_interval_sec: float = 30
    max_attempts: int = 5
    days: int = 7
    since_min: int = 60


class SyncPaymentsBody(BaseModel):
    account_id: str = ""
    username: str = ""
    days: int = 7
    wait: bool = True


class BackfillDepositsBody(BaseModel):
    account_id: str = ""
    username: str = ""
    days: int = 7
    all_accounts: bool = False
    wait: bool = True


class MinigameRefreshBody(BaseModel):
    account_id: str
    game_key: str = "taixiu_dai_loc"
    force: bool = False
    ws_only: bool = False


class MissionRefreshBody(BaseModel):
    """Refresh trạng thái 161 + MINI (mission/list → DB), giống nút Refresh LC79."""

    status: str = Field(
        "Đang Chơi",
        description='Lọc acc CMS; "" = mọi acc',
    )
    account_ids: list[str] = Field(default_factory=list)
    check_only: bool = Field(
        True,
        description="True: chỉ mission/list, không nhận thưởng",
    )
    force_login: bool = False
    parallel: int = Field(8, ge=1, le=32)
    wait: bool = Field(True, description="False: chạy nền")


class MissionAutoClaimBody(BaseModel):
    """CMS nút Ck — luồng auto-mission (rút + nhận, log [AUTO-MISSION] trên main.py)."""

    account_ids: list[str] = Field(..., min_length=1)
    wait: bool = Field(True, description="False: chạy nền")


class VipRefreshBody(BaseModel):
    """vipList + activityreward → account_vip."""

    status: str = Field("Đang Chơi", description='Lọc acc CMS; "" = mọi acc')
    account_ids: list[str] = Field(default_factory=list)
    check_only: bool = Field(
        False,
        description="True: chỉ vipList, không nhận thưởng",
    )
    force_login: bool = False
    parallel: int = Field(8, ge=1, le=32)
    wait: bool = Field(True, description="False: chạy nền")


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ui-config")
def api_ui_config() -> dict[str, Any]:
    """Cấu hình cho giao diện web (cùng origin — không cần CMS proxy)."""
    key = _load_api_key()
    return {
        "needs_api_key": bool(key),
        "api_key": key,
        "default_fund_password": DEFAULT_FUND_PASSWORD,
        "default_login_password": DEFAULT_LOGIN_PASSWORD,
    }


_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


@app.get("/")
def serve_ui() -> FileResponse:
    index = _WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(404, "Thiếu web/index.html")
    return FileResponse(index, headers=_NO_CACHE_HEADERS)


@app.get("/bulk_register.example.csv")
def serve_bulk_example() -> FileResponse:
    path = _DIR / "bulk_register.example.csv"
    if not path.is_file():
        raise HTTPException(404, "Thiếu bulk_register.example.csv")
    return FileResponse(path, filename="bulk_register.example.csv")


def _attach_mission_fields(row: dict[str, Any], *, mission_idx: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from xoso66_mission_db import mission_cms_fields, mission_snapshot_for_account

    snap = mission_snapshot_for_account(
        str(row.get("id") or ""),
        str(row.get("username") or ""),
        index=mission_idx,
    )
    row.update(mission_cms_fields(snap))
    return row


def _attach_vip_fields(row: dict[str, Any], *, vip_idx: dict[str, dict[str, Any]]) -> dict[str, Any]:
    from xoso66_vip_db import vip_cms_fields, vip_snapshot_for_account

    snap = vip_snapshot_for_account(
        str(row.get("id") or ""),
        str(row.get("username") or ""),
        index=vip_idx,
    )
    row.update(vip_cms_fields(snap))
    return row


def _attach_payment_totals(
    row: dict[str, Any], *, pay_idx: dict[str, dict[str, float]]
) -> dict[str, Any]:
    aid = str(row.get("id") or "").strip()
    t = pay_idx.get(aid) or {}
    dep = float(t.get("deposit") if t else (row.get("total_deposit") or 0))
    wd = float(t.get("withdraw") if t else (row.get("total_withdraw") or 0))
    bal = float(row.get("balance") or 0)
    row["total_deposit"] = dep
    row["total_withdraw"] = wd
    row["profit"] = wd + bal - dep
    return row


@app.get("/api/ws-priority-accounts", dependencies=[Depends(require_api_key)])
def api_ws_priority_accounts() -> dict[str, Any]:
    """
    Danh sách ưu tiên mở WS / nạp (ws_fill_priority: 2 = cược↓ rồi số dư↓; 1 = balance↓ rồi cược↑; 0 = balance↑ rồi cược↓).
    """
    from xoso66_config_util import load_config
    from xoso66_ws_pool import list_ws_priority_accounts_payload

    cfg = load_config()
    return list_ws_priority_accounts_payload(cfg)


@app.get("/api/accounts", dependencies=[Depends(require_api_key)])
def api_list_accounts(secrets: bool = False) -> list[dict[str, Any]]:
    from xoso66_mission_db import mission_index_by_account
    from xoso66_payment_history_db import payment_totals_by_account
    from xoso66_vip_db import vip_index_by_account

    mission_idx = mission_index_by_account()
    vip_idx = vip_index_by_account()
    pay_idx = payment_totals_by_account()
    rows = list_accounts()
    out = [public_row(r, include_secrets=secrets) for r in rows]
    return [
        _attach_payment_totals(
            _attach_vip_fields(
                _attach_mission_fields(r, mission_idx=mission_idx),
                vip_idx=vip_idx,
            ),
            pay_idx=pay_idx,
        )
        for r in out
    ]


@app.get("/api/accounts/{account_id}", dependencies=[Depends(require_api_key)])
def api_get_account(account_id: str, secrets: bool = False) -> dict[str, Any]:
    from xoso66_mission_db import mission_index_by_account
    from xoso66_payment_history_db import payment_totals_by_account
    from xoso66_vip_db import vip_index_by_account

    row = get_account(account_id)
    if not row:
        raise HTTPException(404, "Không tìm thấy account")
    out = public_row(row, include_secrets=secrets)
    return _attach_payment_totals(
        _attach_vip_fields(
            _attach_mission_fields(out, mission_idx=mission_index_by_account()),
            vip_idx=vip_index_by_account(),
        ),
        pay_idx=payment_totals_by_account(),
    )


@app.post("/api/accounts", dependencies=[Depends(require_api_key)])
def api_create_account(body: AccountCreate, secrets: bool = False) -> dict[str, Any]:
    try:
        row = create_account(body.model_dump(exclude_none=True))
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    out = public_row(row, include_secrets=secrets)
    from xoso66_cms_chrome import device_proxy_mismatch

    mismatch = device_proxy_mismatch(row)
    if mismatch:
        out["proxy_mismatch"] = mismatch
    return out


@app.put("/api/accounts/{account_id}", dependencies=[Depends(require_api_key)])
def api_update_account(
    account_id: str, body: AccountUpdate, secrets: bool = False
) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    try:
        row, sync_steps = save_account_with_site_sync(account_id, payload)
    except KeyError:
        raise HTTPException(404, "Không tìm thấy account") from None
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    out = public_row(row, include_secrets=secrets)
    if sync_steps:
        out["sync_steps"] = sync_steps
    return out


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(require_api_key)])
def api_delete_account(account_id: str) -> dict[str, bool]:
    if not delete_account(account_id):
        raise HTTPException(404, "Không tìm thấy account")
    return {"deleted": True}


@app.post(
    "/api/accounts/{account_id}/refresh-balance",
    dependencies=[Depends(require_api_key)],
)
def api_refresh_account_balance(account_id: str) -> dict[str, Any]:
    """GET getBalance site → cập nhật accounts.balance + session."""
    from xoso66_session import ensure_session, refresh_account_balance_to_db

    aid = str(account_id or "").strip()
    if not aid:
        raise HTTPException(400, "account_id trống")
    try:
        session = ensure_session(aid, force_login=False)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    rep = refresh_account_balance_to_db(aid, session, refresh=True)
    if not rep.get("ok"):
        raise HTTPException(400, str(rep.get("error") or "getBalance thất bại"))
    row = get_account(aid) or {}
    return {
        "ok": True,
        "account_id": aid,
        "username": row.get("username"),
        "balance": rep.get("balance"),
    }


@app.post(
    "/api/accounts/{account_id}/sync-from-chrome",
    dependencies=[Depends(require_api_key)],
)
def api_sync_from_chrome(
    account_id: str, body: SyncChromeBody | None = None
) -> dict[str, Any]:
    """
    Đồng bộ cf_clearance + headers từ Chrome CMS profile vào session DB.
    Nếu chưa có clearance: mở Chrome CMS — giải captcha trong cửa sổ đó rồi gọi lại.
    """
    aid = str(account_id or "").strip()
    if not aid:
        raise HTTPException(400, "account_id trống")
    b = body or SyncChromeBody()
    try:
        from xoso66_cf import CfRateLimitError
        from xoso66_session import sync_session_from_chrome

        out = sync_session_from_chrome(
            aid,
            device=b.device,
            force_login=bool(b.force_login),
            timeout_sec=int(b.timeout_sec or 0),
        )
    except KeyError:
        raise HTTPException(404, "Không tìm thấy account") from None
    except CfRateLimitError as e:
        raise HTTPException(429, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    if not out.get("ok"):
        msg = str(
            out.get("msg")
            or out.get("error")
            or "Đồng bộ thất bại — mở Chrome CMS, giải captcha Cloudflare, thử lại"
        )
        if out.get("rate_limited"):
            raise HTTPException(429, msg)
        raise HTTPException(400, msg)
    return out


@app.post("/api/accounts/provision", dependencies=[Depends(require_api_key)])
def api_provision(body: ProvisionBody) -> dict[str, Any]:
    step = body.step or "all"
    print(
        f"[PROVISION] step={step} username={body.username} proxy={body.proxy[:20]}…",
        flush=True,
    )
    try:
        out = provision_account(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        print(f"[PROVISION] Lỗi không mong đợi: {e}", flush=True)
        raise HTTPException(500, str(e)) from e
    acc = out.get("account")
    if isinstance(acc, dict):
        out["account"] = public_row(acc, include_secrets=True)
    return out


@app.post("/api/accounts/provision/bulk", dependencies=[Depends(require_api_key)])
def api_provision_bulk(body: BulkProvisionBody) -> dict[str, Any]:
    """Đăng ký lần lượt từng dòng (JSON)."""
    if not body.rows:
        raise HTTPException(400, "rows rỗng")
    if len(body.rows) > 200:
        raise HTTPException(400, "Tối đa 200 dòng / lần")
    print(f"[BULK] {len(body.rows)} tài khoản, delay={body.delay_seconds}s", flush=True)
    try:
        return provision_bulk(
            body.rows,
            defaults=body.defaults,
            stop_on_error=body.stop_on_error,
            delay_seconds=body.delay_seconds,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/accounts/provision/bulk/parse", dependencies=[Depends(require_api_key)])
def api_provision_bulk_parse(body: dict[str, Any]) -> dict[str, Any]:
    """Parse nội dung file (field text) → preview rows."""
    text = str(body.get("text") or "")
    rows = parse_bulk_text(text)
    return {"count": len(rows), "rows": rows}


@app.post("/api/accounts/provision/bulk-file", dependencies=[Depends(require_api_key)])
async def api_provision_bulk_file(
    file: UploadFile = File(...),
    stop_on_error: bool = False,
    delay_seconds: float = 3.0,
    default_password: str = "",
    default_fund_password: str = "",
) -> dict[str, Any]:
    """Upload CSV/TXT → đăng ký lần lượt."""
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    rows = parse_bulk_text(text)
    if not rows:
        raise HTTPException(400, "File không có dòng hợp lệ")
    if len(rows) > 200:
        raise HTTPException(400, "Tối đa 200 dòng / file")
    defaults: dict[str, Any] = {}
    if default_password:
        defaults["password"] = default_password
    if default_fund_password:
        defaults["fund_password"] = default_fund_password
    return provision_bulk(
        rows,
        defaults=defaults or None,
        stop_on_error=stop_on_error,
        delay_seconds=delay_seconds,
    )


def _sync_payment_history_bg(account_id: str, days: int = 7) -> None:
    try:
        from xoso66_accounts_db import username_for_log
        from xoso66_payment_history_sync import sync_account_payment_history

        user = username_for_log(account_id)
        rep = sync_account_payment_history(account_id, days=days)
        if rep.get("ok"):
            print(
                f"[PAYMENT-HIST] {user}: nạp {rep.get('deposit', 0)} | "
                f"rút {rep.get('withdraw', 0)} dòng",
                flush=True,
            )
        else:
            print(
                f"[PAYMENT-HIST] {user}: {rep.get('error') or rep.get('errors')}",
                flush=True,
            )
    except Exception as e:
        from xoso66_accounts_db import username_for_log

        print(f"[PAYMENT-HIST] {username_for_log(account_id)}: {e}", flush=True)


def _deposit_poll_and_open_ws(account_id: str, rep: dict[str, Any]) -> None:
    """Poll lịch sử nạp → Hoàn tất → mở WS (CMS / handler)."""
    from xoso66_config_util import load_config
    from xoso66_ws_pool import _wait_deposit_confirmed, open_ws_after_deposit_confirmed

    cfg = load_config()
    aid = str(account_id).strip()
    if not aid or not rep.get("ok"):
        return
    pending = str(rep.get("status") or rep.get("message") or "").upper()
    is_pending = "PENDING" in pending or "CHỜ" in pending
    if is_pending and not _wait_deposit_confirmed(cfg, aid, rep):
        return
    open_ws_after_deposit_confirmed([aid], cfg)


def _run_deposit(account_id: str, amount: int) -> dict[str, Any]:
    from xoso66_accounts_db import username_for_log
    from xoso66_config_util import load_config

    print(
        f"[DEPOSIT] bắt đầu account_id={account_id} amount={amount}",
        flush=True,
    )
    cfg = load_config()
    ad = cfg.get("auto_deposit") if isinstance(cfg.get("auto_deposit"), dict) else {}
    use_handler = bool(ad.get("handler_enabled", True))

    if use_handler:
        from xoso66_auto_deposit import perform_deposit

        out = perform_deposit(account_id, amount, verbose=True)
    else:
        from xoso66_deposit import create_deposit_order

        out = create_deposit_order(account_id, amount)

    print(
        f"[DEPOSIT] {username_for_log(account_id)} {amount}: ok={out.get('ok')}",
        flush=True,
    )
    if out.get("ok"):
        threading.Thread(
            target=_sync_payment_history_bg,
            args=(account_id,),
            kwargs={"days": 3},
            daemon=True,
        ).start()
        threading.Thread(
            target=_deposit_poll_and_open_ws,
            args=(account_id, out),
            daemon=True,
            name=f"deposit-ws-{account_id}",
        ).start()
    return out


def _run_withdraw(
    account_id: str, amount: int, fund_password: str, use_browser: bool
) -> dict[str, Any]:
    from xoso66_withdraw import run_withdraw_with_tracking

    return run_withdraw_with_tracking(
        account_id,
        amount,
        fund_password or "",
        use_playwright=use_browser,
    )


def _run_refresh(account_id: str, force_login: bool) -> None:
    try:
        from xoso66_accounts_db import username_for_log
        from xoso66_session import ensure_session, session_health

        user = username_for_log(account_id)
        acc = ensure_session(account_id, force_login=force_login)
        h = session_health(acc)
        print(f"[SESSION] {user}: {h}", flush=True)
    except Exception as e:
        from xoso66_accounts_db import username_for_log

        print(f"[SESSION] {username_for_log(account_id)} Lỗi: {e}", flush=True)


@app.post("/api/deposit", dependencies=[Depends(require_api_key)])
def api_deposit(body: DepositBody, bg: BackgroundTasks) -> dict[str, Any]:
    """Nạp QRPay — wait=True trả pay_url / QR (mặc định cho UI)."""
    print(
        f"[API] /api/deposit account_id={body.account_id} amount={body.amount} wait={body.wait}",
        flush=True,
    )
    if body.wait:
        try:
            out = _run_deposit(body.account_id, body.amount)
            return _enrich_deposit_for_ui(out)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(_run_deposit, body.account_id, body.amount)
    return {"ok": True, "message": f"Đang nạp {body.amount} cho {body.account_id}"}


@app.get(
    "/api/accounts/{account_id}/payment-history",
    dependencies=[Depends(require_api_key)],
)
def api_payment_history(
    account_id: str,
    type: int | None = None,
    status: int | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Lịch sử nạp/rút đã lưu DB. type: 1=nạp, 2=rút."""
    if not get_account(account_id):
        raise HTTPException(404, "Không tìm thấy account")
    from xoso66_payment_history_db import list_payment_orders_db

    order_type = type if type in (1, 2) else None
    st = status if status is not None else None
    return list_payment_orders_db(
        account_id,
        order_type=order_type,
        status=st,
        page=page,
        limit=limit,
    )


@app.post(
    "/api/accounts/{account_id}/payment-history/sync",
    dependencies=[Depends(require_api_key)],
)
def api_payment_history_sync(
    account_id: str,
    bg: BackgroundTasks,
    days: int = 7,
    wait: bool = False,
    type: int | None = None,
) -> dict[str, Any]:
    """Kéo paymentorderlist từ site → DB. type: 1=nạp, 2=rút, bỏ trống=cả hai."""
    if not get_account(account_id):
        raise HTTPException(404, "Không tìm thấy account")
    from xoso66_payment_history_db import ORDER_TYPE_DEPOSIT, ORDER_TYPE_WITHDRAW

    if type == 1:
        types = (ORDER_TYPE_DEPOSIT,)
    elif type == 2:
        types = (ORDER_TYPE_WITHDRAW,)
    else:
        types = (ORDER_TYPE_DEPOSIT, ORDER_TYPE_WITHDRAW)
    if wait:
        from xoso66_payment_history_sync import sync_account_payment_history

        try:
            return sync_account_payment_history(
                account_id, days=max(1, days), types=types
            )
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(_sync_payment_history_bg, account_id, max(1, days))
    return {
        "ok": True,
        "message": f"Đang đồng bộ lịch sử {account_id} ({days} ngày)",
    }


def _resolve_account_id(account_id: str = "", username: str = "") -> str:
    from xoso66_accounts_db import get_account_by_username

    aid = str(account_id or "").strip()
    if aid and get_account(aid):
        return aid
    uname = str(username or "").strip()
    if uname:
        acc = get_account_by_username(uname)
        if acc:
            return str(acc.get("id") or "")
    if aid:
        raise HTTPException(404, f"Không tìm thấy account: {aid}")
    raise HTTPException(400, "Cần account_id hoặc username")


@app.get("/api/payment-orders/stats", dependencies=[Depends(require_api_key)])
def api_payment_orders_stats(
    account_id: str | None = None,
    username: str | None = None,
) -> dict[str, Any]:
    """Tổng nạp/rút (Hoàn tất DB), số dư, lợi nhuận = rút + dư − nạp."""
    from xoso66_payment_history_db import payment_stats

    return payment_stats(account_id=account_id, username=username)


@app.get("/api/payment-orders", dependencies=[Depends(require_api_key)])
def api_payment_orders_list(
    account_id: str | None = None,
    username: str | None = None,
    type: int | None = None,
    status: int | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Lịch sử nạp/rút trong DB — toàn CMS. type: 1=nạp, 2=rút."""
    from xoso66_payment_history_db import list_payment_orders_all_db

    order_type = type if type in (1, 2) else None
    st = status if status is not None else None
    return list_payment_orders_all_db(
        account_id=account_id,
        username=username,
        order_type=order_type,
        status=st,
        page=page,
        limit=limit,
    )


@app.get("/api/deposit-orders", dependencies=[Depends(require_api_key)])
def api_deposit_orders_list(
    account_id: str | None = None,
    username: str | None = None,
    status: str | None = None,
    page: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Hàng đợi / lệnh auto nạp (deposit_orders)."""
    from xoso66_deposit_orders_db import list_deposit_orders_db

    return list_deposit_orders_db(
        account_id=account_id,
        username=username,
        status=status,
        page=page,
        limit=limit,
    )


_DEPOSIT_ORDER_STATUSES = frozenset(
    {
        "Chờ Nạp",
        "Đã tạo đơn",
        "Chờ bên thứ 3",
        "Đã Nạp",
        "Thành Công",
        "Thất Bại",
        "Huỷ",
        "Hủy",
    }
)


class DepositOrderPatchBody(BaseModel):
    status: str | None = None
    site_status: int | None = None
    site_status_formatted: str | None = None


_DEPOSIT_SITE_LABEL_TO_STATUS: dict[str, int | None] = {
    "": None,
    "—": None,
    "-": None,
    "Hoàn tất": 1,
    "Đang xử lý": 0,
    "Thất bại": 2,
}


@app.put("/api/deposit-orders/{order_id}", dependencies=[Depends(require_api_key)])
def api_update_deposit_order_status(
    order_id: int, body: DepositOrderPatchBody
) -> dict[str, Any]:
    """CMS — đổi trạng thái lệnh / cột Site trong deposit_orders."""
    from xoso66_auto_deposit import release_deposit_order_tracking
    from xoso66_deposit_orders_db import (
        DEPOSIT_ORDER_TERMINAL_STATUSES,
        get_deposit_order,
        update_deposit_order,
    )

    row = get_deposit_order(int(order_id))
    if not row:
        raise HTTPException(404, "Không tìm thấy lệnh nạp")

    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "Thiếu trường cập nhật (status / site_status / site_status_formatted)")

    updates: dict[str, Any] = {}

    if "status" in data:
        st = str(data.get("status") or "").strip()
        if st not in _DEPOSIT_ORDER_STATUSES:
            raise HTTPException(
                400,
                f"status không hợp lệ: {st!r} (cho phép: {', '.join(sorted(_DEPOSIT_ORDER_STATUSES))})",
            )
        updates["status"] = st

    site_touched = "site_status" in data or "site_status_formatted" in data
    if site_touched:
        fmt_raw = data.get("site_status_formatted")
        fmt = "" if fmt_raw is None else str(fmt_raw).strip()
        if fmt in ("—", "-"):
            fmt = ""
        if fmt == "" and "site_status_formatted" in data:
            updates["site_status"] = None
            updates["site_status_formatted"] = ""
        else:
            if fmt:
                updates["site_status_formatted"] = fmt
            if "site_status" in data:
                updates["site_status"] = data.get("site_status")
            elif fmt:
                if fmt not in _DEPOSIT_SITE_LABEL_TO_STATUS:
                    raise HTTPException(
                        400,
                        f"site không hợp lệ: {fmt!r} (cho phép: Hoàn tất, Đang xử lý, Thất bại, —)",
                    )
                updates["site_status"] = _DEPOSIT_SITE_LABEL_TO_STATUS[fmt]

    if not updates:
        raise HTTPException(400, "Không có trường hợp lệ để cập nhật")

    update_deposit_order(int(order_id), **updates)

    new_st = str(updates.get("status") or row.get("status") or "").strip()
    if new_st in DEPOSIT_ORDER_TERMINAL_STATUSES:
        release_deposit_order_tracking(str(row.get("account_id") or ""))

    out = get_deposit_order(int(order_id))
    return out if out else {"id": order_id, **updates}


@app.post("/api/payment-orders/sync", dependencies=[Depends(require_api_key)])
def api_payment_orders_sync(
    body: SyncPaymentsBody,
    bg: BackgroundTasks,
) -> dict[str, Any]:
    """Đồng bộ paymentorderlist site → DB."""
    aid = _resolve_account_id(body.account_id, body.username)
    days = max(1, int(body.days))
    if body.wait:
        from xoso66_payment_history_sync import sync_account_payment_history

        try:
            return sync_account_payment_history(aid, days=days)
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(_sync_payment_history_bg, aid, days)
    return {"ok": True, "message": f"Đang đồng bộ {aid} ({days} ngày)"}


@app.post("/api/payment-orders/backfill-deposits", dependencies=[Depends(require_api_key)])
def api_backfill_deposit_successes(
    body: BackfillDepositsBody,
    bg: BackgroundTasks,
) -> dict[str, Any]:
    """Bù đơn nạp Hoàn tất (serial mới) vào payment_orders — sửa thống kê lợi nhuận."""
    from xoso66_payment_history_sync import (
        backfill_all_accounts_deposit_successes,
        backfill_deposit_successes_for_account,
    )

    days = max(1, int(body.days))
    if body.all_accounts:
        if body.wait:
            try:
                return backfill_all_accounts_deposit_successes(days=days)
            except Exception as e:
                raise HTTPException(400, str(e)) from e

        def _bg_all() -> None:
            try:
                rep = backfill_all_accounts_deposit_successes(days=days, verbose=True)
                print(
                    f"[BACKFILL-DEP] xong — +{rep.get('deposit_new_total', 0)} serial "
                    f"({rep.get('accounts_ok', 0)}/{rep.get('accounts', 0)} acc)",
                    flush=True,
                )
            except Exception as e:
                print(f"[BACKFILL-DEP] lỗi: {e}", flush=True)

        bg.add_task(_bg_all)
        return {"ok": True, "message": f"Đang bù nạp mọi acc ({days} ngày)"}

    aid = _resolve_account_id(body.account_id, body.username)
    if body.wait:
        try:
            return backfill_deposit_successes_for_account(aid, days=days, verbose=True)
        except Exception as e:
            raise HTTPException(400, str(e)) from e

    def _bg_one(a: str = aid, d: int = days) -> None:
        try:
            rep = backfill_deposit_successes_for_account(a, days=d, verbose=True)
            if int(rep.get("deposit_new") or 0):
                print(
                    f"[BACKFILL-DEP] {rep.get('username') or a}: "
                    f"+{rep.get('deposit_new')} serial",
                    flush=True,
                )
        except Exception as e:
            print(f"[BACKFILL-DEP] {a}: {e}", flush=True)

    bg.add_task(_bg_one)
    return {"ok": True, "message": f"Đang bù nạp {aid} ({days} ngày)"}


@app.post("/api/payment-orders/check-withdraw", dependencies=[Depends(require_api_key)])
def api_payment_orders_check_withdraw(body: CheckWithdrawBody) -> dict[str, Any]:
    """Kéo list rút từ site (proxy) + sync DB; tùy chọn xác nhận theo amount/serial."""
    from xoso66_check_withdraw_history import check_withdraw_history

    key = str(body.account_id or body.username or "").strip()
    if not key:
        raise HTTPException(400, "Cần account_id hoặc username")
    import time

    since_ms = None
    if body.amount:
        since_ms = int(time.time() * 1000) - max(1, int(body.since_min)) * 60 * 1000
    try:
        rep = check_withdraw_history(
            key,
            limit=10,
            days=max(1, int(body.days)),
            amount_vnd=body.amount,
            since_ms=since_ms,
            serial_no=body.serial_no,
            sync_db=True,
            poll=body.poll,
            poll_interval_sec=body.poll_interval_sec,
            max_attempts=body.max_attempts,
            return_details=True,
            verbose=False,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    if not rep.get("ok") and rep.get("error"):
        raise HTTPException(400, str(rep.get("error")))
    return rep


class AutoDepositBody(BaseModel):
    account_id: str
    amount: int = 0
    reason: str = "CMS"


@app.post("/api/auto-deposit", dependencies=[Depends(require_api_key)])
def api_auto_deposit(body: AutoDepositBody) -> dict[str, Any]:
    """Xếp lệnh auto nạp (100k mặc định) — handler + bên thứ 3."""
    from xoso66_auto_deposit import can_create_deposit_order, default_amount, enqueue_deposit

    print(
        f"[API] /api/auto-deposit account_id={body.account_id} amount={body.amount} reason={body.reason!r}",
        flush=True,
    )
    if not get_account(body.account_id):
        raise HTTPException(404, "Không tìm thấy account")
    if not can_create_deposit_order(body.account_id):
        raise HTTPException(409, "Đang có lệnh nạp chưa xong hoặc cache")
    amt = int(body.amount) if body.amount > 0 else default_amount()
    enqueue_deposit(body.account_id, body.reason or f"CMS {amt:,}đ")
    return {
        "ok": True,
        "message": f"Đã xếp nạp {amt:,}đ cho {body.account_id}",
        "amount": amt,
    }


@app.post("/api/withdraw", dependencies=[Depends(require_api_key)])
def api_withdraw(body: WithdrawBody, bg: BackgroundTasks) -> dict[str, Any]:
    """Rút tiền — wait=True trả kết quả (mặc định cho UI)."""
    if body.wait:
        try:
            return _run_withdraw(
                body.account_id,
                body.amount,
                body.fund_password or "",
                body.use_browser,
            )
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(
        _run_withdraw,
        body.account_id,
        body.amount,
        body.fund_password or "",
        body.use_browser,
    )
    return {"ok": True, "message": f"Đang rút {body.amount} cho {body.account_id}"}


_MISSION_REFRESH_LOCK = threading.Lock()
_VIP_REFRESH_LOCK = threading.Lock()


def _run_mission_refresh(
    *,
    status: str = "Đang Chơi",
    account_ids: list[str] | None = None,
    check_only: bool = True,
    force_login: bool = False,
    parallel: int = 8,
) -> dict[str, Any]:
    from xoso66_daily_mission_check import refresh_missions_batch

    if not _MISSION_REFRESH_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "busy": True,
            "error": "Đang có lượt refresh mission khác — chờ xong rồi thử",
            "results": [],
        }

    try:
        st = str(status or "").strip() or None
        aids = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
        return refresh_missions_batch(
            account_ids=aids if aids else None,
            status_filter=st,
            check_only=check_only,
            force_login=force_login,
            parallel=parallel,
        )
    finally:
        _MISSION_REFRESH_LOCK.release()


@app.post("/api/missions/refresh-status", dependencies=[Depends(require_api_key)])
def api_missions_refresh_status(
    body: MissionRefreshBody, bg: BackgroundTasks
) -> dict[str, Any]:
    """🔄 Refresh CMS: mission/list → cập nhật 161 + MINI GAME trong DB."""
    if body.wait:
        try:
            out = _run_mission_refresh(
                status=body.status,
                account_ids=body.account_ids,
                check_only=body.check_only,
                force_login=body.force_login,
                parallel=body.parallel,
            )
            if out.get("busy"):
                raise HTTPException(409, str(out.get("error") or "mission refresh busy"))
            return out
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(
        _run_mission_refresh,
        status=body.status,
        account_ids=body.account_ids,
        check_only=body.check_only,
        force_login=body.force_login,
        parallel=body.parallel,
    )
    n = len(body.account_ids) if body.account_ids else "theo status"
    return {
        "ok": True,
        "message": f"Đang refresh mission ({n}) — check_only={body.check_only}",
    }


@app.post("/api/missions/auto-claim", dependencies=[Depends(require_api_key)])
def api_missions_auto_claim(
    body: MissionAutoClaimBody, bg: BackgroundTasks
) -> dict[str, Any]:
    """Nút Ck CMS — cùng luồng worker auto-mission (log [AUTO-MISSION] trên terminal main.py)."""
    from xoso66_auto_mission_reward import run_manual_auto_mission_claim

    aids = [str(x).strip() for x in body.account_ids if str(x).strip()]
    if not aids:
        raise HTTPException(400, "account_ids bắt buộc")
    if body.wait:
        try:
            out = run_manual_auto_mission_claim(aids)
            if out.get("busy"):
                raise HTTPException(409, str(out.get("error") or "auto-mission busy"))
            return out
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(run_manual_auto_mission_claim, aids)
    return {"ok": True, "message": f"Đang auto-mission (Ck) cho {len(aids)} acc"}


def _run_vip_refresh(
    *,
    status: str = "Đang Chơi",
    account_ids: list[str] | None = None,
    check_only: bool = False,
    force_login: bool = False,
    parallel: int = 8,
) -> dict[str, Any]:
    from xoso66_vip_check import refresh_vip_batch

    if not _VIP_REFRESH_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "busy": True,
            "error": "Đang có lượt refresh VIP khác — chờ xong rồi thử",
            "results": [],
        }
    try:
        st = str(status or "").strip() or None
        aids = [str(x).strip() for x in (account_ids or []) if str(x).strip()]
        return refresh_vip_batch(
            account_ids=aids if aids else None,
            status_filter=st,
            check_only=check_only,
            force_login=force_login,
            parallel=parallel,
        )
    finally:
        _VIP_REFRESH_LOCK.release()


@app.post("/api/vip/refresh-status", dependencies=[Depends(require_api_key)])
def api_vip_refresh_status(body: VipRefreshBody, bg: BackgroundTasks) -> dict[str, Any]:
    """Check + nhận VIP (vipList → account_vip)."""
    if body.wait:
        try:
            out = _run_vip_refresh(
                status=body.status,
                account_ids=body.account_ids,
                check_only=body.check_only,
                force_login=body.force_login,
                parallel=body.parallel,
            )
            if out.get("busy"):
                raise HTTPException(409, str(out.get("error") or "vip refresh busy"))
            return out
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, str(e)) from e
    bg.add_task(
        _run_vip_refresh,
        status=body.status,
        account_ids=body.account_ids,
        check_only=body.check_only,
        force_login=body.force_login,
        parallel=body.parallel,
    )
    n = len(body.account_ids) if body.account_ids else "theo status"
    return {
        "ok": True,
        "message": f"Đang refresh VIP ({n}) — check_only={body.check_only}",
    }


@app.post("/api/refresh-session", dependencies=[Depends(require_api_key)])
def api_refresh_session(body: RefreshSessionBody, bg: BackgroundTasks) -> dict[str, Any]:
    bg.add_task(_run_refresh, body.account_id, body.force_login)
    return {"ok": True, "message": f"Đang refresh session {body.account_id}"}


@app.post("/api/minigame/refresh", dependencies=[Depends(require_api_key)])
def api_minigame_refresh(body: MinigameRefreshBody) -> dict[str, Any]:
    """Lấy / refresh user-token + ws token mini-game (lưu DB)."""
    try:
        from xoso66_minigame_refresh import refresh_minigame_tokens

        return refresh_minigame_tokens(
            {},
            account_id=body.account_id,
            game_key=body.game_key,
            force=body.force,
            ws_only=body.ws_only,
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


def _mount_static_assets() -> None:
    from fastapi.staticfiles import StaticFiles

    from xoso66_deposit import QR_OUTPUT_DIR

    QR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/qr_outputs",
        StaticFiles(directory=str(QR_OUTPUT_DIR)),
        name="qr_outputs",
    )


def run_api_server(host: str, port: int) -> None:
    import uvicorn

    from xoso66_shutdown import clear_api_server, register_api_server, stopping

    configure_stdio_utf8()
    _mount_static_assets()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    register_api_server(server)
    try:
        server.run()
    finally:
        clear_api_server()
        if stopping():
            print("[API] Đã dừng.", flush=True)


def main() -> None:
    from xoso66_config_util import load_config

    cfg = load_config()
    host = os.environ.get("XOSO66_API_HOST") or str(cfg.get("api_host") or "127.0.0.1")
    port = int(os.environ.get("XOSO66_API_PORT") or cfg.get("api_port") or 8799)
    run_api_server(host, port)


if __name__ == "__main__":
    main()
