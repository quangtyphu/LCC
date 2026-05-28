# Phân tích WS hoangnam47 — bàn C06 (tableID 1006)

**CDP:** `http://127.0.0.1:9361`  
**Capture:** `vendor_ws_20260525T222338Z.jsonl` (45s, 2026-05-25 22:23 UTC+7)

## Kết luận

| Hạng mục | Giá trị |
|----------|---------|
| Vào bàn UI | `singleBacTable.jsp` — `healthy: true`, `onTable: true` |
| WS host | `wss://bpcdf.mhuxu.com/h54uk/?token=…` |
| Bàn C06 | `tableID=1006` |
| Sự kiện in ra | `KET QUA PHIEN` ván 13 (Hòa 8-8), `PHIEN MOI` shoe 20034 ván 14 |

## Cấu trúc WS Bikimex (đã xác nhận trên tài khoản này)

1. **Frame binary:** prefix `0xD9` + 1 byte length + JSON UTF-8.
2. **Khi ngồi bàn C06:** chủ yếu `messageType: GameInfo`.
3. **Phiên mới:** `handler: 4`, `eventType: GP_NEW_GAME_START`, `gameShoe`, `gameRound`, `tableID: 1006`.
4. **Kết quả ván:** `handler: 4`, `eventType: GP_WINNER`, `winner` (0=Con, 1=Cái, 2=Hòa, …), `playerHandValue`, `bankerHandValue`.
5. **Cập nhật pool cược:** `GameInfo` `handler: 1` (không in PHIEN/KQ — chỉ bet pool).
6. **Sảnh (chưa vào bàn):** `GameHallInfo` — broadcast nhiều bàn; vẫn có GP_* nhưng lọc `tableID=1006`.

## Lỗi đã sửa

- `summarize_message()` dùng `re.Pattern | re.Pattern` → crash mọi frame.
- `ws_listen` giờ **cùng luồng `c168_vendor_auto_bet`**: parse JSON → `_extract_event()` → in PHIEN MOI / KET QUA PHIEN.

## Chạy lại

Chỉ nghe WS (file chuẩn):

```powershell
python c168_listen_ws.py -u hoangnam47 --table-id 1006
```

Mở Chrome + vào bàn + nghe:

```powershell
python c168_post_open_chrome.py -u hoangnam47
```
