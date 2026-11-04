import os
import io
import uuid
import socket
from datetime import datetime
from functools import wraps

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    send_from_directory,
    send_file,
    jsonify,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side

from database import (
    init_db,
    get_db,
    get_next_doc_number,
    format_thai_date,
    DOC_TYPES,
    UPLOAD_FOLDER,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "gdms-secret-key-coj-intranet-2026")
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB max upload size

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_ext(filename: str) -> str:
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)

    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            flash("คุณไม่มีสิทธิ์เข้าถึงส่วนของผู้ดูแลระบบ", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)

    return decorated_function


@app.context_processor
def inject_globals():
    conn = get_db()
    settings_rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    settings = {r["key"]: r["value"] for r in settings_rows}
    current_byear = datetime.now().year + 543

    # Count reserved vs stored for quick sidebar badge
    reserved_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM documents WHERE status = 'reserved'"
    ).fetchone()["cnt"]
    conn.close()

    return dict(
        DOC_TYPES=DOC_TYPES,
        format_thai_date=format_thai_date,
        current_byear=current_byear,
        sys_settings=settings,
        reserved_count=reserved_count,
        today_iso=datetime.now().strftime("%Y-%m-%d"),
    )


# -------------------------------------------------------------------------
# Authentication Routes
# -------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()

        if user and user["is_active"] == 1 and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["full_name"] = user["full_name"]
            session["position"] = user["position"]
            session["role"] = user["role"]
            flash(f"ยินดีต้อนรับ {user['full_name']}", "success")
            next_page = request.args.get("next")
            return redirect(next_page or url_for("archive"))
        else:
            flash("ชื่อผู้ใช้งานหรือรหัสผ่านไม่ถูกต้อง หรือบัญชีถูกระงับ", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("ออกจากระบบเรียบร้อยแล้ว", "info")
    return redirect(url_for("login"))


@app.route("/profile/password", methods=["POST"])
@login_required
def change_own_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "").strip()
    confirm_password = request.form.get("confirm_password", "").strip()

    if not new_password or len(new_password) < 4:
        flash("รหัสผ่านใหม่ต้องมีความยาวอย่างน้อย 4 ตัวอักษร", "warning")
        return redirect(request.referrer or url_for("archive"))

    if new_password != confirm_password:
        flash("รหัสผ่านใหม่และการยืนยันรหัสผ่านไม่ตรงกัน", "danger")
        return redirect(request.referrer or url_for("archive"))

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not user or not check_password_hash(user["password_hash"], current_password):
        conn.close()
        flash("รหัสผ่านปัจจุบันไม่ถูกต้อง", "danger")
        return redirect(request.referrer or url_for("archive"))

    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), session["user_id"]),
    )
    conn.commit()
    conn.close()
    flash("เปลี่ยนรหัสผ่านสำเร็จเรียบร้อยแล้ว", "success")
    return redirect(request.referrer or url_for("archive"))


# -------------------------------------------------------------------------
# Main Pages
# -------------------------------------------------------------------------
@app.route("/")
@login_required
def index():
    return redirect(url_for("archive"))


@app.route("/api/next-number")
@login_required
def api_next_number():
    doc_type = request.args.get("doc_type", DOC_TYPES[0])
    doc_date = request.args.get("doc_date", datetime.now().strftime("%Y-%m-%d"))
    try:
        dt = datetime.strptime(doc_date, "%Y-%m-%d")
        doc_year = dt.year + 543
    except Exception:
        doc_year = datetime.now().year + 543

    conn = get_db()
    next_num = get_next_doc_number(conn, doc_type, doc_year)
    conn.close()
    return jsonify(
        {
            "doc_type": doc_type,
            "doc_year": doc_year,
            "next_number": next_num,
            "formatted": f"{next_num}/{doc_year}",
        }
    )


@app.route("/create", methods=["GET", "POST"])
@login_required
def create_document():
    conn = get_db()

    if request.method == "POST":
        doc_type = request.form.get("doc_type", "").strip()
        title = request.form.get("title", "").strip()
        doc_date = request.form.get("doc_date", "").strip()
        note = request.form.get("note", "").strip()

        if doc_type not in DOC_TYPES:
            conn.close()
            flash("กรุณาเลือกประเภทคำสั่งหรือประกาศให้ถูกต้อง", "danger")
            return redirect(url_for("create_document"))

        if not title:
            conn.close()
            flash("กรุณากรอกเรื่องของคำสั่งหรือประกาศ", "danger")
            return redirect(url_for("create_document"))

        try:
            dt = datetime.strptime(doc_date, "%Y-%m-%d")
            doc_year = dt.year + 543
        except Exception:
            conn.close()
            flash("รูปแบบวันที่ไม่ถูกต้อง", "danger")
            return redirect(url_for("create_document"))

        # Check file upload
        uploaded_file = request.files.get("attachment")
        file_path = None
        original_filename = None
        file_type = None
        file_size = 0
        status = "reserved"

        if uploaded_file and uploaded_file.filename:
            if not allowed_file(uploaded_file.filename):
                conn.close()
                flash("รองรับเฉพาะไฟล์ PDF และรูปภาพ (.pdf, .jpg, .jpeg, .png) เท่านั้น", "danger")
                return redirect(url_for("create_document"))

            ext = get_file_ext(uploaded_file.filename)
            original_filename = uploaded_file.filename
            year_dir = os.path.join(UPLOAD_FOLDER, str(doc_year))
            os.makedirs(year_dir, exist_ok=True)

            unique_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
            full_save_path = os.path.join(year_dir, unique_name)
            uploaded_file.save(full_save_path)

            file_path = f"{doc_year}/{unique_name}"
            file_type = "pdf" if ext == "pdf" else "image"
            file_size = os.path.getsize(full_save_path)
            status = "stored"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                doc_number = get_next_doc_number(conn, doc_type, doc_year)
                conn.execute(
                    """
                    INSERT INTO documents (
                        doc_type, doc_number, doc_year, title, doc_date, status,
                        file_path, original_filename, file_type, file_size, note,
                        created_by, updated_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        doc_type,
                        doc_number,
                        doc_year,
                        title,
                        doc_date,
                        status,
                        file_path,
                        original_filename,
                        file_type,
                        file_size,
                        note,
                        session["user_id"],
                        session["user_id"],
                        now_str,
                        now_str,
                    ),
                )
            status_label = "จัดเก็บพร้อมไฟล์แนบ" if status == "stored" else "จองเลขคำสั่ง"
            flash(
                f"ออกเลข {doc_type} ที่ {doc_number}/{doc_year} ({status_label}) เรียบร้อยแล้ว",
                "success",
            )
            conn.close()
            return redirect(url_for("archive"))
        except Exception as e:
            conn.close()
            flash(f"เกิดข้อผิดพลาดในการบันทึกข้อมูล: {e}", "danger")
            return redirect(url_for("create_document"))

    # Calculate next numbers preview for all 4 types in current year
    current_byear = datetime.now().year + 543
    next_numbers = {}
    for dt in DOC_TYPES:
        next_numbers[dt] = get_next_doc_number(conn, dt, current_byear)
    conn.close()

    return render_template("create.html", next_numbers=next_numbers)


@app.route("/archive")
@login_required
def archive():
    conn = get_db()

    # Optional query filters
    doc_type_filter = request.args.get("doc_type", "").strip()
    status_filter = request.args.get("status", "").strip()
    year_filter = request.args.get("year", "").strip()

    query = """
        SELECT d.*, u.full_name as creator_name, u2.full_name as updater_name
        FROM documents d
        LEFT JOIN users u ON d.created_by = u.id
        LEFT JOIN users u2 ON d.updated_by = u2.id
        WHERE 1=1
    """
    params = []

    if doc_type_filter:
        query += " AND d.doc_type = ?"
        params.append(doc_type_filter)
    if status_filter:
        query += " AND d.status = ?"
        params.append(status_filter)
    if year_filter:
        query += " AND d.doc_year = ?"
        params.append(int(year_filter))

    query += " ORDER BY d.id DESC"
    docs = conn.execute(query, params).fetchall()

    # Available years for filter dropdown
    years_rows = conn.execute(
        "SELECT DISTINCT doc_year FROM documents ORDER BY doc_year DESC"
    ).fetchall()
    available_years = [r["doc_year"] for r in years_rows]
    current_byear = datetime.now().year + 543
    if current_byear not in available_years:
        available_years.insert(0, current_byear)

    # Quick stats for current year
    stats_by_type = {}
    for dt in DOC_TYPES:
        row = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'stored' THEN 1 ELSE 0 END) as stored_cnt,
                SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END) as reserved_cnt,
                MAX(doc_number) as latest_num
            FROM documents
            WHERE doc_type = ? AND doc_year = ?
            """,
            (dt, current_byear),
        ).fetchone()
        stats_by_type[dt] = {
            "total": row["total"] or 0,
            "stored": row["stored_cnt"] or 0,
            "reserved": row["reserved_cnt"] or 0,
            "latest_num": row["latest_num"] or 0,
            "next_num": get_next_doc_number(conn, dt, current_byear),
        }

    conn.close()
    return render_template(
        "archive.html",
        documents=docs,
        available_years=available_years,
        stats_by_type=stats_by_type,
        doc_type_filter=doc_type_filter,
        status_filter=status_filter,
        year_filter=year_filter,
    )


@app.route("/document/<int:doc_id>/edit", methods=["POST"])
@login_required
def edit_document(doc_id):
    conn = get_db()
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        flash("ไม่พบรายการเอกสารที่ต้องการแก้ไข", "danger")
        return redirect(url_for("archive"))

    # Permission check: Admin or Creator
    if session.get("role") != "admin" and doc["created_by"] != session["user_id"]:
        conn.close()
        flash("คุณไม่มีสิทธิ์แก้ไขเอกสารรายการนี้ (แก้ไขได้เฉพาะผู้บันทึก หรือผู้ดูแลระบบ)", "danger")
        return redirect(url_for("archive"))

    title = request.form.get("title", "").strip()
    doc_date = request.form.get("doc_date", "").strip()
    note = request.form.get("note", "").strip()
    remove_file = request.form.get("remove_file") == "1"

    if not title:
        conn.close()
        flash("กรุณากรอกเรื่องของคำสั่งหรือประกาศ", "danger")
        return redirect(url_for("archive"))

    try:
        datetime.strptime(doc_date, "%Y-%m-%d")
    except Exception:
        conn.close()
        flash("รูปแบบวันที่ไม่ถูกต้อง", "danger")
        return redirect(url_for("archive"))

    file_path = doc["file_path"]
    original_filename = doc["original_filename"]
    file_type = doc["file_type"]
    file_size = doc["file_size"]
    status = doc["status"]

    uploaded_file = request.files.get("attachment")
    if uploaded_file and uploaded_file.filename:
        if not allowed_file(uploaded_file.filename):
            conn.close()
            flash("รองรับเฉพาะไฟล์ PDF และรูปภาพ (.pdf, .jpg, .jpeg, .png) เท่านั้น", "danger")
            return redirect(url_for("archive"))

        # Delete old file if exists
        if file_path:
            old_abs = os.path.join(UPLOAD_FOLDER, file_path)
            if os.path.exists(old_abs):
                try:
                    os.remove(old_abs)
                except OSError:
                    pass

        ext = get_file_ext(uploaded_file.filename)
        original_filename = uploaded_file.filename
        year_dir = os.path.join(UPLOAD_FOLDER, str(doc["doc_year"]))
        os.makedirs(year_dir, exist_ok=True)

        unique_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
        full_save_path = os.path.join(year_dir, unique_name)
        uploaded_file.save(full_save_path)

        file_path = f"{doc['doc_year']}/{unique_name}"
        file_type = "pdf" if ext == "pdf" else "image"
        file_size = os.path.getsize(full_save_path)
        status = "stored"

    elif remove_file and file_path:
        old_abs = os.path.join(UPLOAD_FOLDER, file_path)
        if os.path.exists(old_abs):
            try:
                os.remove(old_abs)
            except OSError:
                pass
        file_path = None
        original_filename = None
        file_type = None
        file_size = 0
        status = "reserved"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """
        UPDATE documents
        SET title = ?, doc_date = ?, status = ?, file_path = ?,
            original_filename = ?, file_type = ?, file_size = ?, note = ?,
            updated_by = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            title,
            doc_date,
            status,
            file_path,
            original_filename,
            file_type,
            file_size,
            note,
            session["user_id"],
            now_str,
            doc_id,
        ),
    )
    conn.commit()
    conn.close()

    flash(
        f"อัปเดตข้อมูล {doc['doc_type']} เลขที่ {doc['doc_number']}/{doc['doc_year']} เรียบร้อยแล้ว",
        "success",
    )
    return redirect(request.referrer or url_for("archive"))


@app.route("/document/<int:doc_id>/delete", methods=["POST"])
@admin_required
def delete_document(doc_id):
    conn = get_db()
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not doc:
        conn.close()
        flash("ไม่พบรายการเอกสาร", "danger")
        return redirect(url_for("archive"))

    if doc["file_path"]:
        abs_path = os.path.join(UPLOAD_FOLDER, doc["file_path"])
        if os.path.exists(abs_path):
            try:
                os.remove(abs_path)
            except OSError:
                pass

    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()

    flash(
        f"ลบรายการ {doc['doc_type']} เลขที่ {doc['doc_number']}/{doc['doc_year']} เรียบร้อยแล้ว",
        "info",
    )
    return redirect(request.referrer or url_for("archive"))


# -------------------------------------------------------------------------
# File Serving & Preview
# -------------------------------------------------------------------------
@app.route("/files/<path:filename>")
@login_required
def serve_file(filename):
    download = request.args.get("download") == "1"
    conn = get_db()
    doc = conn.execute(
        "SELECT original_filename FROM documents WHERE file_path = ?", (filename,)
    ).fetchone()
    conn.close()

    if download and doc and doc["original_filename"]:
        return send_from_directory(
            UPLOAD_FOLDER,
            filename,
            as_attachment=True,
            download_name=doc["original_filename"],
        )
    return send_from_directory(UPLOAD_FOLDER, filename)


# -------------------------------------------------------------------------
# Reports, Dashboard, Print & Excel Export
# -------------------------------------------------------------------------
def get_filtered_report_docs(conn, args):
    doc_type = args.get("doc_type", "").strip()
    status = args.get("status", "").strip()
    year = args.get("year", "").strip()
    start_date = args.get("start_date", "").strip()
    end_date = args.get("end_date", "").strip()
    keyword = args.get("keyword", "").strip()

    query = """
        SELECT d.*, u.full_name as creator_name
        FROM documents d
        LEFT JOIN users u ON d.created_by = u.id
        WHERE 1=1
    """
    params = []

    if doc_type:
        query += " AND d.doc_type = ?"
        params.append(doc_type)
    if status:
        query += " AND d.status = ?"
        params.append(status)
    if year:
        query += " AND d.doc_year = ?"
        params.append(int(year))
    if start_date:
        query += " AND d.doc_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND d.doc_date <= ?"
        params.append(end_date)
    if keyword:
        query += " AND (d.title LIKE ? OR u.full_name LIKE ? OR CAST(d.doc_number AS TEXT) LIKE ?)"
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw])

    query += " ORDER BY d.doc_year DESC, d.doc_type ASC, d.doc_number ASC"
    return conn.execute(query, params).fetchall()


@app.route("/reports")
@login_required
def reports():
    conn = get_db()
    current_byear = datetime.now().year + 543
    selected_year = request.args.get("year", str(current_byear)).strip()

    years_rows = conn.execute(
        "SELECT DISTINCT doc_year FROM documents ORDER BY doc_year DESC"
    ).fetchall()
    available_years = [r["doc_year"] for r in years_rows]
    if current_byear not in available_years:
        available_years.insert(0, current_byear)

    docs = get_filtered_report_docs(conn, request.args)

    # Summary dashboard stats for the selected year (or all if year is empty)
    year_val = int(selected_year) if selected_year.isdigit() else current_byear
    summary_cards = []
    total_all = 0
    total_stored = 0
    total_reserved = 0

    for dt in DOC_TYPES:
        row = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'stored' THEN 1 ELSE 0 END) as stored_cnt,
                SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END) as reserved_cnt,
                MAX(doc_number) as max_num
            FROM documents
            WHERE doc_type = ? AND doc_year = ?
            """,
            (dt, year_val),
        ).fetchone()
        t = row["total"] or 0
        s = row["stored_cnt"] or 0
        r = row["reserved_cnt"] or 0
        total_all += t
        total_stored += s
        total_reserved += r
        summary_cards.append(
            {
                "doc_type": dt,
                "total": t,
                "stored": s,
                "reserved": r,
                "max_num": row["max_num"] or 0,
            }
        )

    conn.close()
    return render_template(
        "reports.html",
        documents=docs,
        available_years=available_years,
        summary_cards=summary_cards,
        total_all=total_all,
        total_stored=total_stored,
        total_reserved=total_reserved,
        selected_year=year_val,
        filters=request.args,
    )


@app.route("/reports/export-excel")
@login_required
def export_excel():
    conn = get_db()
    docs = get_filtered_report_docs(conn, request.args)
    settings_rows = conn.execute("SELECT key, value FROM system_settings").fetchall()
    settings = {r["key"]: r["value"] for r in settings_rows}
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ทะเบียนคุมคำสั่งและประกาศ"

    # Styling
    title_font = Font(name="TH SarabunPSK", size=16, bold=True)
    header_font = Font(name="TH SarabunPSK", size=14, bold=True, color="FFFFFF")
    cell_font = Font(name="TH SarabunPSK", size=14)
    header_fill = PatternFill(start_color="0F766E", end_color="0F766E", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )

    org_name = settings.get("org_name", "ศาลยุติธรรม")
    doc_type_title = request.args.get("doc_type") or "ทุกประเภทคำสั่งและประกาศ"
    ws.merge_cells("A1:H1")
    ws["A1"] = f"สมุดทะเบียนคุม{doc_type_title} - {org_name}"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")

    headers = [
        "ลำดับ",
        "เลขที่คำสั่ง/ประกาศ",
        "ประเภท",
        "เรื่อง",
        "ลงวันที่",
        "สถานะ",
        "ผู้บันทึก",
        "หมายเหตุ",
    ]
    ws.append([])  # Row 2 blank
    ws.append(headers)  # Row 3 headers

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=3, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for idx, d in enumerate(docs, start=1):
        status_th = "จัดเก็บแล้ว" if d["status"] == "stored" else "จองเลข"
        row_data = [
            idx,
            f"{d['doc_number']}/{d['doc_year']}",
            d["doc_type"],
            d["title"],
            format_thai_date(d["doc_date"], short=False),
            status_th,
            d["creator_name"] or "-",
            d["note"] or "",
        ]
        ws.append(row_data)
        curr_row = ws.max_row
        for col_idx in range(1, len(headers) + 1):
            c = ws.cell(row=curr_row, column=col_idx)
            c.font = cell_font
            c.border = thin_border
            if col_idx in (1, 2, 3, 5, 6):
                c.alignment = Alignment(horizontal="center", vertical="top")
            else:
                c.alignment = Alignment(horizontal="left", vertical="top", wrap_text=True)

    # Set column widths
    col_widths = [8, 18, 20, 60, 22, 16, 26, 25]
    for i, w in enumerate(col_widths, start=1):
        col_letter = openpyxl.utils.get_column_letter(i)
        ws.column_dimensions[col_letter].width = w

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"ทะเบียนคำสั่งและประกาศ_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/reports/print")
@login_required
def print_report():
    conn = get_db()
    docs = get_filtered_report_docs(conn, request.args)
    conn.close()
    return render_template(
        "print_report.html",
        documents=docs,
        filters=request.args,
        printed_at=format_thai_date(datetime.now().strftime("%Y-%m-%d"), short=False),
    )


# -------------------------------------------------------------------------
# Admin Routes
# -------------------------------------------------------------------------
@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    conn = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            full_name = request.form.get("full_name", "").strip()
            position = request.form.get("position", "").strip()
            role = request.form.get("role", "user").strip()

            if not username or not password or not full_name:
                flash("กรุณากรอกข้อมูลผู้ใช้งานให้ครบถ้วน", "danger")
            else:
                try:
                    conn.execute(
                        """
                        INSERT INTO users (username, password_hash, full_name, position, role, is_active, created_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            username,
                            generate_password_hash(password),
                            full_name,
                            position,
                            role if role in ("admin", "user") else "user",
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        ),
                    )
                    conn.commit()
                    flash(f"เพิ่มผู้ใช้งาน {full_name} ({username}) เรียบร้อยแล้ว", "success")
                except Exception:
                    flash(f"ชื่อผู้ใช้งาน '{username}' มีอยู่ในระบบแล้ว", "danger")

        elif action == "edit":
            user_id = int(request.form.get("user_id", 0))
            full_name = request.form.get("full_name", "").strip()
            position = request.form.get("position", "").strip()
            role = request.form.get("role", "user").strip()
            is_active = 1 if request.form.get("is_active") == "1" else 0
            new_password = request.form.get("new_password", "").strip()

            if user_id == session["user_id"] and is_active == 0:
                flash("ไม่สามารถระงับบัญชีของตนเองที่กำลังใช้งานอยู่ได้", "warning")
            else:
                conn.execute(
                    "UPDATE users SET full_name = ?, position = ?, role = ?, is_active = ? WHERE id = ?",
                    (full_name, position, role, is_active, user_id),
                )
                if new_password:
                    conn.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (generate_password_hash(new_password), user_id),
                    )
                conn.commit()
                flash(f"อัปเดตข้อมูลของ {full_name} เรียบร้อยแล้ว", "success")

        conn.close()
        return redirect(url_for("admin_users"))

    users = conn.execute(
        """
        SELECT u.*,
               (SELECT COUNT(*) FROM documents d WHERE d.created_by = u.id) as doc_count
        FROM users u
        ORDER BY u.id ASC
        """
    ).fetchall()
    conn.close()
    return render_template("admin_users.html", users=users)


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    conn = get_db()
    current_byear = datetime.now().year + 543

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_numbers":
            target_year = int(request.form.get("doc_year", current_byear))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for dt in DOC_TYPES:
                start_val = max(1, int(request.form.get(f"start_{dt}", 1)))
                conn.execute(
                    """
                    INSERT INTO number_configs (doc_type, doc_year, start_number, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(doc_type, doc_year) DO UPDATE SET
                        start_number = excluded.start_number,
                        updated_at = excluded.updated_at
                    """,
                    (dt, target_year, start_val, now_str),
                )
            conn.commit()
            flash(f"บันทึกการตั้งค่าเลขเริ่มต้นประจำปี พ.ศ. {target_year} เรียบร้อยแล้ว", "success")

        elif action == "update_org":
            org_name = request.form.get("org_name", "").strip()
            system_title = request.form.get("system_title", "").strip()
            if org_name:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('org_name', ?)",
                    (org_name,),
                )
            if system_title:
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value) VALUES ('system_title', ?)",
                    (system_title,),
                )
            conn.commit()
            flash("บันทึกข้อมูลหน่วยงานเรียบร้อยแล้ว", "success")

        conn.close()
        return redirect(url_for("admin_settings", year=request.form.get("doc_year", current_byear)))

    selected_year = int(request.args.get("year", current_byear))
    configs = {}
    for dt in DOC_TYPES:
        cfg_row = conn.execute(
            "SELECT start_number FROM number_configs WHERE doc_type = ? AND doc_year = ?",
            (dt, selected_year),
        ).fetchone()
        max_row = conn.execute(
            "SELECT MAX(doc_number) as max_num, COUNT(*) as cnt FROM documents WHERE doc_type = ? AND doc_year = ?",
            (dt, selected_year),
        ).fetchone()

        start_num = cfg_row["start_number"] if cfg_row else 1
        max_num = max_row["max_num"] if (max_row and max_row["max_num"]) else 0
        next_num = get_next_doc_number(conn, dt, selected_year)

        configs[dt] = {
            "start_number": start_num,
            "max_number": max_num,
            "total_docs": max_row["cnt"] if max_row else 0,
            "next_number": next_num,
        }

    conn.close()
    return render_template(
        "admin_settings.html",
        configs=configs,
        selected_year=selected_year,
    )


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    init_db()
    lan_ip = get_lan_ip()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 65)
    print("  ระบบจัดเก็บคำสั่งและประกาศศาล (Official Order Numbering System)")
    print(f"  เข้าใช้งานบนเครื่องนี้:   http://localhost:{port}")
    print(f"  เข้าใช้งานผ่านวง LAN:  http://{lan_ip}:{port}")
    print("  บัญชีเริ่มต้น: admin / admin1234  หรือ  sirinapa / 123456")
    print("=" * 65)
    app.run(host="0.0.0.0", port=port, debug=False)
