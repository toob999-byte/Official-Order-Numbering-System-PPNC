import os
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "gdms.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")

DOC_TYPES = [
    "คำสั่งศาล",
    "คำสั่งสำนักงาน",
    "ประกาศศาล",
    "ประกาศสำนักงาน",
]

THAI_MONTHS_SHORT = [
    "",
    "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."
]

THAI_MONTHS_FULL = [
    "",
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
]


def ensure_dirs():
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)


def get_db():
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db():
    conn = get_db()
    with conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT NOT NULL,
                position TEXT DEFAULT '',
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_type TEXT NOT NULL,
                doc_number INTEGER NOT NULL,
                doc_year INTEGER NOT NULL,
                title TEXT NOT NULL,
                doc_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'reserved',
                file_path TEXT DEFAULT NULL,
                original_filename TEXT DEFAULT NULL,
                file_type TEXT DEFAULT NULL,
                file_size INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                created_by INTEGER NOT NULL,
                updated_by INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (created_by) REFERENCES users(id),
                FOREIGN KEY (updated_by) REFERENCES users(id),
                UNIQUE(doc_type, doc_year, doc_number)
            );

            CREATE TABLE IF NOT EXISTS number_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_type TEXT NOT NULL,
                doc_year INTEGER NOT NULL,
                start_number INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                UNIQUE(doc_type, doc_year)
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        # Default settings
        cur = conn.execute("SELECT COUNT(*) as cnt FROM system_settings")
        if cur.fetchone()["cnt"] == 0:
            default_settings = [
                ("org_name", "ศาลจังหวัดปากพนัง"),
                ("system_title", "ระบบสารบรรณอิเล็กทรอนิกส์ (GDMS)"),
                ("system_subtitle", "ระบบจัดเก็บคำสั่งและประกาศศาล"),
            ]
            conn.executemany(
                "INSERT INTO system_settings (key, value) VALUES (?, ?)",
                default_settings,
            )

        # Default users if empty
        cur = conn.execute("SELECT COUNT(*) as cnt FROM users")
        if cur.fetchone()["cnt"] == 0:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            users_seed = [
                (
                    "admin",
                    generate_password_hash("admin1234"),
                    "ผู้ดูแลระบบ (Admin)",
                    "นักวิชาการคอมพิวเตอร์",
                    "admin",
                    1,
                    now_str,
                ),
                (
                    "sirinapa",
                    generate_password_hash("123456"),
                    "นางสาวศิรินภา คงชื่น",
                    "เจ้าพนักงานศาลยุติธรรม",
                    "user",
                    1,
                    now_str,
                ),
                (
                    "kanokkan",
                    generate_password_hash("123456"),
                    "นางสาวกนกกาญจน์ คำทอง",
                    "นักวิชาการเงินและบัญชี",
                    "user",
                    1,
                    now_str,
                ),
                (
                    "adisak",
                    generate_password_hash("123456"),
                    "นายอดิศักดิ์ กิจจารักษ์",
                    "เจ้าหน้าที่ศาลยุติธรรม",
                    "user",
                    1,
                    now_str,
                ),
            ]
            conn.executemany(
                """
                INSERT INTO users (username, password_hash, full_name, position, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                users_seed,
            )

            # Seed current Buddhist year number configs
            current_byear = datetime.now().year + 543
            for dt in DOC_TYPES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO number_configs (doc_type, doc_year, start_number, updated_at)
                    VALUES (?, ?, 1, ?)
                    """,
                    (dt, current_byear, now_str),
                )
    conn.close()


def get_next_doc_number(conn, doc_type: str, doc_year: int) -> int:
    """Calculate the next sequential number for a given doc_type and Buddhist year."""
    cur = conn.execute(
        "SELECT start_number FROM number_configs WHERE doc_type = ? AND doc_year = ?",
        (doc_type, doc_year),
    )
    row = cur.fetchone()
    start_num = row["start_number"] if row else 1

    cur_max = conn.execute(
        "SELECT MAX(doc_number) as max_num FROM documents WHERE doc_type = ? AND doc_year = ?",
        (doc_type, doc_year),
    )
    max_row = cur_max.fetchone()
    max_num = max_row["max_num"] if (max_row and max_row["max_num"] is not None) else 0

    return max(start_num, max_num + 1)


def format_thai_date(date_str: str, short: bool = True) -> str:
    """Convert YYYY-MM-DD to Thai date e.g. '30 ต.ค. 69' or '30 ตุลาคม 2569'."""
    if not date_str:
        return "-"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        day = dt.day
        month_idx = dt.month
        byear = dt.year + 543
        if short:
            byear_short = str(byear)[-2:]
            return f"{day} {THAI_MONTHS_SHORT[month_idx]} {byear_short}"
        else:
            return f"{day} {THAI_MONTHS_FULL[month_idx]} {byear}"
    except Exception:
        return date_str
