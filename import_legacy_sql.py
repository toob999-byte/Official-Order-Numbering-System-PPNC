import os
import re
from datetime import datetime
from werkzeug.security import generate_password_hash

from database import init_db, get_db, BASE_DIR, UPLOAD_FOLDER, DOC_TYPES

DEFAULT_SQL_PATH = os.path.join(
    BASE_DIR, "ExportDB_GDMS04022569", "SQL_laragon04022569.sql"
)


def parse_mysql_values(block_str: str):
    """Parse MySQL VALUES (...), (...) into list of tuples."""
    rows = []
    i = 0
    n = len(block_str)
    while i < n:
        while i < n and block_str[i] != "(":
            i += 1
        if i >= n:
            break
        i += 1  # skip '('
        fields = []
        while i < n:
            while i < n and block_str[i] in " \t\r\n":
                i += 1
            if i >= n:
                break
            if block_str[i] == "'":
                i += 1
                buf = []
                while i < n:
                    ch = block_str[i]
                    if ch == "\\" and i + 1 < n:
                        nxt = block_str[i + 1]
                        if nxt == "n":
                            buf.append("\n")
                        elif nxt == "r":
                            buf.append("\r")
                        elif nxt == "t":
                            buf.append("\t")
                        else:
                            buf.append(nxt)
                        i += 2
                    elif ch == "'":
                        if i + 1 < n and block_str[i + 1] == "'":
                            buf.append("'")
                            i += 2
                        else:
                            i += 1
                            break
                    else:
                        buf.append(ch)
                        i += 1
                fields.append("".join(buf))
            else:
                start = i
                while i < n and block_str[i] not in (",", ")"):
                    i += 1
                token = block_str[start:i].strip()
                if token.upper() == "NULL":
                    fields.append(None)
                else:
                    try:
                        fields.append(int(token))
                    except ValueError:
                        try:
                            fields.append(float(token))
                        except ValueError:
                            fields.append(token)
            while i < n and block_str[i] in " \t\r\n":
                i += 1
            if i < n and block_str[i] == ",":
                i += 1
            elif i < n and block_str[i] == ")":
                i += 1
                break
        rows.append(tuple(fields))
    return rows


def extract_table_rows(sql_text: str, table_name: str):
    blocks = re.findall(
        rf"INSERT INTO `{table_name}`.*?VALUES\s*(.*?);\r?\n",
        sql_text,
        re.DOTALL,
    )
    rows = []
    for b in blocks:
        rows.extend(parse_mysql_values(b))
    return rows


def import_legacy_sql(sql_path: str = DEFAULT_SQL_PATH):
    if not os.path.exists(sql_path):
        raise FileNotFoundError(f"ไม่พบไฟล์ SQL: {sql_path}")

    print(f"กำลังอ่านไฟล์ฐานข้อมูลเดิม: {sql_path} ...")
    with open(sql_path, "r", encoding="utf-8", errors="replace") as f:
        sql_text = f.read()

    courts_rows = extract_table_rows(sql_text, "courts")
    users_rows = extract_table_rows(sql_text, "users")
    user_role_rows = extract_table_rows(sql_text, "user_role")
    keeptypes_rows = extract_table_rows(sql_text, "keeptypes")
    keeps_rows = extract_table_rows(sql_text, "keeps")

    type_map = {r[0]: r[1].strip() for r in keeptypes_rows}
    # In legacy GDMS: role_id 7 = Admin, role_id 3 = Adminsarabun
    admin_user_ids = {r[3] for r in user_role_rows if r[4] in (7, 3)}
    admin_user_ids.add(1)  # user id 1 is admin

    init_db()
    conn = get_db()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with conn:
        conn.execute("PRAGMA foreign_keys = OFF;")
        conn.execute("DELETE FROM documents;")
        conn.execute("DELETE FROM users;")
        conn.execute("DELETE FROM number_configs;")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('users', 'documents');")

        # 1. Update Organization Name from `courts`
        if courts_rows and courts_rows[0][1]:
            court_name = courts_rows[0][1].strip()
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('org_name', ?)",
                (court_name,),
            )

        # 2. Import Users (preserving original IDs, usernames, names, positions, and bcrypt hashes)
        imported_users = 0
        for u in users_rows:
            # (id, name, position, section_id, username, email, password, isAdmin, level, avatar, remember_token, created_at, updated_at, commit, status, courts_id, Ltoken)
            uid = u[0]
            full_name = (u[1] or "").strip()
            position = (u[2] or "").strip()
            username = (u[4] or f"user{uid}").strip()
            pwd_hash = u[6] or generate_password_hash("123456")
            created_at = u[11] or now_str
            role = "admin" if (uid in admin_user_ids or u[7] == 1 or username.lower() == "admin") else "user"

            conn.execute(
                """
                INSERT INTO users (id, username, password_hash, full_name, position, role, is_active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (uid, username, pwd_hash, full_name, position, role, created_at),
            )
            imported_users += 1

        # 3. Import Documents (`keeps`)
        imported_docs = 0
        stored_count = 0
        reserved_count = 0

        for kr in keeps_rows:
            # (id, keep_on, type_id, name, keep_at, keep_year, filename, user_id, created_at, updated_at)
            doc_id = kr[0]
            doc_number = int(kr[1])
            type_id = int(kr[2])
            doc_type = type_map.get(type_id, DOC_TYPES[0])
            title = (kr[3] or "").strip()
            doc_date = (kr[4] or "2026-01-01").strip()
            keep_year_ce = int(kr[5]) if kr[5] else int(doc_date[:4])
            doc_year_be = keep_year_ce + 543 if keep_year_ce < 2400 else keep_year_ce
            legacy_filename = kr[6].strip() if kr[6] else None
            creator_id = int(kr[7]) if kr[7] else 1
            created_at = kr[8] or f"{doc_date} 09:00:00"
            updated_at = kr[9] or created_at

            if legacy_filename:
                status = "stored"
                file_path = f"{doc_year_be}/{legacy_filename}"
                original_filename = legacy_filename
                ext = legacy_filename.rsplit(".", 1)[-1].lower() if "." in legacy_filename else "pdf"
                file_type = "pdf" if ext == "pdf" else "image"
                stored_count += 1
            else:
                status = "reserved"
                file_path = None
                original_filename = None
                file_type = None
                reserved_count += 1

            conn.execute(
                """
                INSERT INTO documents (
                    id, doc_type, doc_number, doc_year, title, doc_date, status,
                    file_path, original_filename, file_type, file_size, note,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?, ?, ?)
                """,
                (
                    doc_id,
                    doc_type,
                    doc_number,
                    doc_year_be,
                    title,
                    doc_date,
                    status,
                    file_path,
                    original_filename,
                    file_type,
                    creator_id,
                    creator_id,
                    created_at,
                    updated_at,
                ),
            )
            imported_docs += 1

        # 4. Update number_configs for all imported years
        years_rows = conn.execute("SELECT DISTINCT doc_year FROM documents").fetchall()
        for yr_row in years_rows:
            y = yr_row["doc_year"]
            for dt in DOC_TYPES:
                os.makedirs(os.path.join(UPLOAD_FOLDER, str(y)), exist_ok=True)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO number_configs (doc_type, doc_year, start_number, updated_at)
                    VALUES (?, ?, 1, ?)
                    """,
                    (dt, y, now_str),
                )

        conn.execute("PRAGMA foreign_keys = ON;")

    # Print summary report
    print("=" * 68)
    print("  นำเข้าข้อมูลจากระบบ GDMS เดิมสำเร็จเรียบร้อยแล้ว!")
    print("-" * 68)
    print(f"  * ผู้ใช้งานทั้งหมดที่นำเข้า:      {imported_users} บัญชี (ใช้ Username/Password เดิมได้ทันที)")
    print(f"  * เอกสารคำสั่งและประกาศทั้งหมด:  {imported_docs} รายการ")
    print(f"      - สถานะจัดเก็บแล้ว:          {stored_count} รายการ")
    print(f"      - สถานะจองเลข:              {reserved_count} รายการ")
    print("-" * 68)
    print("  สรุปเลขคำสั่งล่าสุดของปี พ.ศ. 2569 และเลขถัดไปที่จะรันต่อ:")
    for dt in DOC_TYPES:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, MAX(doc_number) as max_num FROM documents WHERE doc_type = ? AND doc_year = 2569",
            (dt,),
        ).fetchone()
        max_n = row["max_num"] or 0
        cnt = row["cnt"] or 0
        print(f"    - {dt:<16}: มี {cnt:>3} เรื่อง | เลขล่าสุด {max_n:>3}/2569 -> เลขถัดไปคือ {max_n + 1}/2569")
    print("=" * 68)
    conn.close()


if __name__ == "__main__":
    import_legacy_sql()
