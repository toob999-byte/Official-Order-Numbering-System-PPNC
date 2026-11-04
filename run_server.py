import os
from waitress import serve
from app import app, get_lan_ip
from database import init_db

if __name__ == "__main__":
    init_db()
    lan_ip = get_lan_ip()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 70)
    print("  [GDMS] ระบบออกเลขและจัดเก็บคำสั่ง/ประกาศศาล (Intranet LAN Server)")
    print("-" * 70)
    print(f"  * เปิดใช้งานบนเครื่องเซิร์ฟเวอร์:  http://localhost:{port}")
    print(f"  * เปิดใช้งานจากเครื่องอื่นใน LAN:  http://{lan_ip}:{port}")
    print("-" * 70)
    print("  บัญชีผู้ดูแลระบบเริ่มต้น:  admin / admin1234")
    print("  บัญชีผู้ใช้งานทั่วไป:      sirinapa / 123456")
    print("=" * 70)
    print("  กด Ctrl+C เพื่อหยุดการทำงานของเซิร์ฟเวอร์")
    serve(app, host="0.0.0.0", port=port, threads=8)
