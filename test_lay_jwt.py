from pymongo import MongoClient

# ⚙️ Config DB
MONGO_URI = "mongodb://127.0.0.1:27017"
DB_NAME = "game_data"
COLLECTION = "userprofiles"

# 📦 Kết nối MongoDB
client = MongoClient(MONGO_URI)
db = client[DB_NAME]
userprofiles = db[COLLECTION]

def get_jwt_by_username(username):
    user = userprofiles.find_one({"username": username})
    if not user:
        print(f"❌ Không tìm thấy tài khoản '{username}' trong DB")
        return None
    
    jwt = user.get("jwt")
    if not jwt:
        print(f"⚠️ Tài khoản '{username}' không có JWT")
        return None

    print(f"✅ JWT của '{username}':\n{jwt}")
    return jwt


if __name__ == "__main__":
    username = input("👤 Nhập username: ").strip()
    get_jwt_by_username(username)
