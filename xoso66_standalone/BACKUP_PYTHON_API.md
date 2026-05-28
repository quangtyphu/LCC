# Backup — API Python (port 8799)

CMS dùng **API Node** + DB **`CMS/game_data/xoso66.db`**.

| File LC79 | Ghi chú |
|-----------|---------|
| `xoso66_api.py` | FastAPI backup (:8799) — không bắt buộc |
| `main.py` | Worker WS / auto-bet — đọc **CMS DB** |
| `data/xoso66.db` | **Không dùng nữa** — backup cũ |
| `xoso66_cms_bridge.py` | CMS gọi nạp/rút/provision |

Chạy worker:

```bash
cd xoso66_standalone
python main.py
# DB: C:\Users\Quang\Documents\CMS\game_data\xoso66.db
```
