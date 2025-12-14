import sqlite3
import json, os

# ================== SQLite setup ==================
DB_PATH = r"C:\Users\Quang\Documents\CMS\game_data.db"

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ================== Lấy toàn bộ user_profiles ==================
def get_all_userprofiles():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_profiles")
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ================== Lấy danh sách account đang active ==================
def get_active_accounts():
    """
    Trả về list account có status = 'Đang Chơi' để manager WS dùng.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_profiles WHERE status = ?", ("Đang Chơi",))
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ================== MAIN test ==================
def main():
    print("📋 Danh sách user_profiles trong game_data.db:\n")
    profiles = get_all_userprofiles()
    if not profiles:
        print("⚠️ Không có user nào trong bảng user_profiles")
    else:
        print(f"{'ID'.ljust(5)}{'Username'.ljust(20)}{'Status'.ljust(15)}{'Balance'.ljust(10)}")
        print("-" * 60)
        for row in profiles:
            print(
                f"{str(row.get('id','')).ljust(5)}"
                f"{str(row.get('username','')).ljust(20)}"
                f"{str(row.get('status','')).ljust(15)}"
                f"{str(row.get('balance','')).ljust(10)}"
            )

    # test hàm get_active_accounts
    print("\n⚡ Active accounts (status='Đang Chơi'):")
    active = get_active_accounts()
    for acc in active:
        print(f"- {acc['username']} | Proxy={acc.get('proxy')} | JWT={acc.get('jwt','')[:20]}...")


if __name__ == "__main__":
    main()
