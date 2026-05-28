# benbet_standalone

Automation cho [benhome1.vip](https://benhome1.vip/pc/home) / BEN Bet (API: `api.bencloud.io`).

## Cài đặt

```bash
cd benbet_standalone
pip install -r requirements.txt
```

## Đăng nhập

```bash
python benbet_login.py -u TEN_DANG_NHAP -p MAT_KHAU
python benbet_login.py -u TEN_DANG_NHAP -p MAT_KHAU --json
python benbet_login.py -u TEN_DANG_NHAP -p MAT_KHAU --proxy host:port:user:pass
```

Dùng trong code:

```python
from benbet_login import login

r = login("username", "password")
if r["ok"]:
    token = r["lt"]
    user = r["user_info"]
```

## Mở game 3D ([play.3dbenbet.net](https://play.3dbenbet.net/))

Sau login: `POST game.bencloud.io/game/get_url` với field **`id`** (id menu, không phải `gamecode`).

| Game | Menu `id` | `gameid` trên URL launch |
|------|-----------|--------------------------|
| Tài Xỉu Cân Bảng | `14001` | `8` |

```bash
python benbet_open_game.py -u USER -p PASS
python benbet_open_game.py -u USER -p PASS --open-browser
python benbet_open_game.py -u USER -p PASS --list-games
```

## Tài Xỉu — phiên mới & cược (so với LC79 / xoso66)

| | LC79 | xoso66 | BEN 3D |
|---|------|--------|--------|
| Transport | Socket.IO `42/tx,...` | WSS minigame | **SignalR** WebSocket |
| Hub | namespace `tx` | subscribe game_id | **`LuckyDiceHub`** |
| Phiên mới | event trong `ws_events.py` | `register_round_start_handler` | **`NOTIFY_CHANGE_PHRASE`**, `SESSION_INFO` |
| Mở game | token site | session file | `get_url` → launch URL + `access_token` |

Luồng kỹ thuật:

1. XHR `GET https://taixiu.3dbenbet.net/signalr/negotiate?...` (tab F12 → **XHR**, không phải `play.3dbenbet.net`)
2. WS `wss://taixiu.3dbenbet.net/signalr/connect?...&connectionData=[{"name":"luckydiceHub"}]`
3. Cược/kết quả phiên: frame JSON trên WS đó — `BET`, `BET_SUCCESS`, `SESSION_INFO`, `sessionResult`
3. Hub gửi JSON `{ "M": [ { "M": "SESSION_INFO", "A": [...] }, ... ] }`

`benbet_game.py`: `open_tai_xiu_session()`, `build_signalr_ws_url()`. Bước tiếp theo: bắt gói `BET` trên WS khi click Tài/Xỉu trong DevTools → implement `benbet_taixiu_ws.py`.
