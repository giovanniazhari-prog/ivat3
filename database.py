import sqlite3
import os
import logging

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    db_path = os.getenv("DB_PATH", "bot_data.db")
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS phone_numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT UNIQUE NOT NULL,
                quality TEXT DEFAULT 'standard',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS seen_otps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                otp_message TEXT NOT NULL,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(phone_number, otp_message)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS allowed_users (
                uid INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            conn.execute("ALTER TABLE phone_numbers ADD COLUMN quality TEXT DEFAULT 'standard'")
        except Exception:
            pass
        conn.commit()
    logger.info("Database initialized")


def get_setting(key: str) -> str | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (key, value),
        )
        conn.commit()


def add_numbers(numbers: list[str], quality: str = "standard") -> tuple[int, int]:
    added = 0
    skipped = 0
    with get_connection() as conn:
        for number in numbers:
            number = number.strip()
            if not number:
                continue
            try:
                conn.execute(
                    "INSERT INTO phone_numbers (number, quality) VALUES (?, ?)",
                    (number, quality)
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
    return added, skipped


def add_numbers_with_quality(entries: list[tuple[str, str]]) -> tuple[int, int]:
    added = 0
    skipped = 0
    with get_connection() as conn:
        for number, quality in entries:
            number = number.strip()
            if not number:
                continue
            try:
                conn.execute(
                    "INSERT INTO phone_numbers (number, quality) VALUES (?, ?)",
                    (number, quality)
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.commit()
    return added, skipped


# Semua quality yang dianggap "bio" (punya bio)
BIO_QUALITIES = ("bio", "bio_lmb", "bio_standart", "bio_eklusif", "bio_suite")
# Semua quality yang punya LMB/bisnis
BIZ_QUALITIES = ("lmb", "standart", "eklusif", "suite",
                  "bio_lmb", "bio_standart", "bio_eklusif", "bio_suite")
# Urutan prioritas untuk gacha
QUALITY_RANK = {
    "bio_eklusif": 1, "bio_suite": 2, "bio_standart": 3, "bio_lmb": 4, "bio": 5,
    "eklusif": 6,     "suite": 7,     "standart": 8,     "lmb": 9,    "standard": 10,
}

def _qual_placeholders(quals: tuple) -> str:
    return ",".join("?" * len(quals))


def get_random_numbers_exclude(
    count: int = 5,
    filter_quality: str = "lmb",
    exclude: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Sama dengan get_random_numbers tapi skip nomor yang ada di `exclude`."""
    exclude = exclude or set()
    with get_connection() as conn:
        quals = BIZ_QUALITIES
        ph    = _qual_placeholders(quals)
        rows  = conn.execute(
            f"SELECT number, quality FROM phone_numbers WHERE quality IN ({ph}) ORDER BY RANDOM() LIMIT ?",
            (*quals, count + len(exclude) + 20)   # ambil lebih banyak lalu filter
        ).fetchall()
    result = [(r["number"], r["quality"]) for r in rows if r["number"] not in exclude]
    return result[:count]


def get_random_numbers(count: int = 5, filter_quality: str = "all") -> list[tuple[str, str]]:
    with get_connection() as conn:
        if filter_quality == "bio":
            # Ada bio (semua tipe)
            ph = _qual_placeholders(BIO_QUALITIES)
            rows = conn.execute(
                f"SELECT number, quality FROM phone_numbers WHERE quality IN ({ph}) ORDER BY RANDOM() LIMIT ?",
                (*BIO_QUALITIES, count)
            ).fetchall()
        elif filter_quality == "bio_lmb":
            # Bio + LMB/bisnis (tier tertinggi)
            quals = ("bio_lmb", "bio_eklusif", "bio_suite", "bio_standart")
            ph = _qual_placeholders(quals)
            rows = conn.execute(
                f"SELECT number, quality FROM phone_numbers WHERE quality IN ({ph}) ORDER BY RANDOM() LIMIT ?",
                (*quals, count)
            ).fetchall()
        elif filter_quality == "lmb":
            # LMB/bisnis semua (bio atau tidak)
            ph = _qual_placeholders(BIZ_QUALITIES)
            rows = conn.execute(
                f"SELECT number, quality FROM phone_numbers WHERE quality IN ({ph}) ORDER BY RANDOM() LIMIT ?",
                (*BIZ_QUALITIES, count)
            ).fetchall()
        elif filter_quality == "mix":
            rows = conn.execute(
                "SELECT number, quality FROM phone_numbers ORDER BY RANDOM() LIMIT ?",
                (count,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT number, quality FROM phone_numbers ORDER BY RANDOM() LIMIT ?",
                (count,)
            ).fetchall()
    return [(row["number"], row["quality"]) for row in rows]


def count_numbers() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) as total FROM phone_numbers").fetchone()
    return row["total"] if row else 0


def count_by_quality() -> dict:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT quality, COUNT(*) as total FROM phone_numbers GROUP BY quality"
        ).fetchall()
    result = {
        "bio_eklusif": 0, "bio_suite": 0, "bio_standart": 0, "bio_lmb": 0, "bio": 0,
        "eklusif": 0,     "suite": 0,     "standart": 0,     "lmb": 0,    "standard": 0,
    }
    for row in rows:
        q = row["quality"] or "standard"
        result[q] = result.get(q, 0) + row["total"]
    return result


def count_by_quality_summary() -> dict:
    """Ringkasan: total_bio, total_biz, total_standard, total_all"""
    q = count_by_quality()
    total_bio = sum(q[k] for k in BIO_QUALITIES if k in q)
    total_biz = sum(q[k] for k in BIZ_QUALITIES if k in q)
    return {
        "total_all":      sum(q.values()),
        "total_bio":      total_bio,
        "total_biz":      total_biz,
        "total_standard": q.get("standard", 0),
        "detail":         q,
    }


def get_allowed_users() -> list[int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT uid FROM allowed_users ORDER BY added_at"
        ).fetchall()
    return [row["uid"] for row in rows]


def add_allowed_user(uid: int) -> bool:
    """Return True kalau berhasil ditambah, False kalau sudah ada."""
    with get_connection() as conn:
        try:
            conn.execute("INSERT INTO allowed_users (uid) VALUES (?)", (uid,))
            conn.commit()
            return True
        except Exception:
            return False


def remove_allowed_user(uid: int) -> bool:
    """Return True kalau berhasil dihapus."""
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM allowed_users WHERE uid = ?", (uid,))
        conn.commit()
    return cur.rowcount > 0


def is_allowed_user(uid: int) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE uid = ?", (uid,)
        ).fetchone()
    return row is not None


def clear_numbers() -> int:
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM phone_numbers")
        conn.commit()
    return cursor.rowcount


def delete_number(number: str) -> bool:
    number = number.strip()
    with get_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM phone_numbers WHERE number = ?", (number,)
        )
        conn.commit()
    return cursor.rowcount > 0


def get_all_numbers_for_export() -> list[tuple[str, str]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT number, quality FROM phone_numbers ORDER BY quality, number"
        ).fetchall()
    return [(row["number"], row["quality"]) for row in rows]


def is_otp_seen(phone_number: str, otp_message: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM seen_otps WHERE phone_number = ? AND otp_message = ?",
            (phone_number, otp_message),
        ).fetchone()
    return row is not None


def mark_otp_seen(phone_number: str, otp_message: str):
    with get_connection() as conn:
        try:
            conn.execute(
                "INSERT INTO seen_otps (phone_number, otp_message) VALUES (?, ?)",
                (phone_number, otp_message),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass


def get_today_otps() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT phone_number, otp_message, seen_at FROM seen_otps "
            "WHERE date(seen_at) = date('now') "
            "ORDER BY seen_at DESC"
        ).fetchall()
    return [
        {
            "phone_number": r["phone_number"],
            "otp_message": r["otp_message"],
            "seen_at": r["seen_at"],
        }
        for r in rows
    ]
