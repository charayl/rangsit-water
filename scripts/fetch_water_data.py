import json
import random
import os

# Path ไปยังไฟล์ network.json
JSON_PATH = os.path.join(os.path.dirname(__file__), '../data/network.json')

def update_water_flow():
    if not os.path.exists(JSON_PATH):
        print(f"Error: File not found at {JSON_PATH}")
        return

    with open(JSON_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # จำลองการดึง API และอัปเดตค่า Q (อัตราการไหล)
    for edge in data['edges']:
        # ตัวอย่าง: สุ่มสภาวะน้ำจริง หรือแมปจาก API Endpoints
        delta = random.randint(-5, 5)
        edge['current_Q_m3s'] = max(-20, edge['current_Q_m3s'] + delta)

    with open(JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("✅ Successfully updated network.json with fresh water flow data!")

if __name__ == "__main__":
    update_water_flow()