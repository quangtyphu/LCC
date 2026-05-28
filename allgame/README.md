# ALLGAME

Đa cổng game (CM88, FLY88, C168, F168, …) → cùng **Game B** (bàn BCR). Cùng cấp với `lc79/`, `xoso66_standalone/`, `c168_standalone/`.

## Cấu trúc

```
allgame/
  main.py                 # entry
  allgame_config.json     # copy từ .example
  db/
    accounts_db.py        # SQLite thống nhất (portal_id + username)
    portals_db.py         # đăng ký cổng game
  portals/
    base.py               # Token / Deposit / Withdraw protocols
    registry.py
    cm88/  fly88/  c168/  f168/
      token.py            # mỗi game 1 file
      deposit.py
      withdraw.py
  orchestrator/           # reconcile 60s, session RAM
  transport/              # Chrome (skeleton)
  vendor/                 # Game B chung (TODO)
```

## DB & CMS

- File: `CMS/game_data/allgame.db` (hoặc `ALLGAME_DB`)
- **Quản Lý Chrome** (`CMS/public/QuanLyChrome.html`): nút **AllGame** → popup chọn cổng game + lưu qua `/api/allgame/accounts`
- API CMS: `CMS/lib/allgame_api.js` — khóa `(portal_id, username)`
- Profile Chrome: `CMS/game_data/allgame_browsers/{portal_id}/{username}/`
- Khóa tài khoản: `(portal_id, username)`
- Trạng thái chuẩn: `Đang Chơi`, `Hết Tiền`, `Đủ ngày`, `Token Lỗi`, …

## Chạy thử

```bash
cd allgame
python main.py --init-db
python main.py --reconcile-once
python main.py
```

## Triển khai từng game

1. Implement `portals/{game}/token.py` (đọc session, test_token)
2. Implement `deposit.py`, `withdraw.py`
3. Nối `transport/chrome_transport.py` → `c168_standalone`
4. `vendor/` — refactor open_game / enter_table / auto_bet
