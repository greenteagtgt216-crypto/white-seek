import sqlite3
from datetime import datetime, timedelta

DB_PATH = "subscriptions.db"

MAX_CHATS_PER_USER = 20  # максимум чатов на одного пользователя


def init_db():
    """Создаёт таблицы при первом запуске."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Подписки
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            vk_id          INTEGER PRIMARY KEY,
            subscribed_at  TEXT,
            expires_at     TEXT,
            is_active      INTEGER DEFAULT 0,
            payment_id     TEXT
        )
    """)

    # Чаты (треды) пользователя
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            vk_id       INTEGER NOT NULL,
            name        TEXT NOT NULL DEFAULT 'Новый чат',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (vk_id) REFERENCES users(vk_id)
        )
    """)

    # Активный чат каждого пользователя
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            vk_id          INTEGER PRIMARY KEY,
            active_chat_id INTEGER,
            FOREIGN KEY (active_chat_id) REFERENCES chats(id)
        )
    """)

    conn.commit()
    conn.close()


# ─────────────────────────────  ПОДПИСКИ  ─────────────────────────────

def add_or_renew_subscription(vk_id: int, payment_id: str, days: int = 30):
    """Активирует или продлевает подписку пользователя."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.utcnow()
    expires = now + timedelta(days=days)
    cursor.execute("""
        INSERT INTO users (vk_id, subscribed_at, expires_at, is_active, payment_id)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(vk_id) DO UPDATE SET
            subscribed_at = excluded.subscribed_at,
            expires_at    = excluded.expires_at,
            is_active     = 1,
            payment_id    = excluded.payment_id
    """, (vk_id, now.isoformat(), expires.isoformat(), payment_id))
    conn.commit()
    conn.close()


def is_subscribed(vk_id: int) -> bool:
    """Возвращает True, если подписка активна и не истекла."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT expires_at, is_active FROM users WHERE vk_id = ?", (vk_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False
    expires_at, is_active = row
    if not is_active:
        return False
    return datetime.fromisoformat(expires_at) > datetime.utcnow()


def get_expiry(vk_id: int) -> str | None:
    """Возвращает дату окончания подписки в читаемом виде."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT expires_at FROM users WHERE vk_id = ?", (vk_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    dt = datetime.fromisoformat(row[0])
    return dt.strftime("%d.%m.%Y %H:%M UTC")


# ─────────────────────────────  ЧАТЫ  ─────────────────────────────

def create_chat(vk_id: int, name: str = "Новый чат") -> int | None:
    """
    Создаёт новый чат и делает его активным.
    Возвращает id созданного чата, или None если достигнут лимит.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Проверяем лимит
    cursor.execute("SELECT COUNT(*) FROM chats WHERE vk_id = ?", (vk_id,))
    count = cursor.fetchone()[0]
    if count >= MAX_CHATS_PER_USER:
        conn.close()
        return None

    now = datetime.utcnow().isoformat()
    cursor.execute(
        "INSERT INTO chats (vk_id, name, created_at) VALUES (?, ?, ?)",
        (vk_id, name[:50], now),
    )
    chat_id = cursor.lastrowid

    # Делаем новый чат активным
    cursor.execute("""
        INSERT INTO user_state (vk_id, active_chat_id)
        VALUES (?, ?)
        ON CONFLICT(vk_id) DO UPDATE SET active_chat_id = excluded.active_chat_id
    """, (vk_id, chat_id))

    conn.commit()
    conn.close()
    return chat_id


def get_active_chat(vk_id: int) -> tuple[int, str] | None:
    """
    Возвращает (chat_id, chat_name) активного чата.
    Если у пользователя нет ни одного чата — создаёт первый автоматически.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT c.id, c.name
        FROM user_state us
        JOIN chats c ON c.id = us.active_chat_id
        WHERE us.vk_id = ?
    """, (vk_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return (row[0], row[1])

    # Нет активного чата — создаём первый
    chat_id = create_chat(vk_id, "Чат 1")
    return (chat_id, "Чат 1") if chat_id else None


def list_chats(vk_id: int) -> list[dict]:
    """
    Возвращает список чатов пользователя.
    Каждый элемент: {'id', 'name', 'created_at', 'is_active'}
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT c.id, c.name, c.created_at,
               CASE WHEN us.active_chat_id = c.id THEN 1 ELSE 0 END AS is_active
        FROM chats c
        LEFT JOIN user_state us ON us.vk_id = c.vk_id
        WHERE c.vk_id = ?
        ORDER BY c.id ASC
    """, (vk_id,))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for i, row in enumerate(rows, 1):
        dt = datetime.fromisoformat(row[2])
        result.append({
            "index":     i,
            "id":        row[0],
            "name":      row[1],
            "created_at": dt.strftime("%d.%m.%Y"),
            "is_active": bool(row[3]),
        })
    return result


def switch_chat(vk_id: int, chat_id: int) -> bool:
    """
    Переключает активный чат пользователя.
    Возвращает True при успехе, False если чат не принадлежит пользователю.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM chats WHERE id = ? AND vk_id = ?", (chat_id, vk_id)
    )
    if not cursor.fetchone():
        conn.close()
        return False

    cursor.execute("""
        INSERT INTO user_state (vk_id, active_chat_id)
        VALUES (?, ?)
        ON CONFLICT(vk_id) DO UPDATE SET active_chat_id = excluded.active_chat_id
    """, (vk_id, chat_id))
    conn.commit()
    conn.close()
    return True


def switch_chat_by_index(vk_id: int, index: int) -> tuple[bool, str]:
    """
    Переключает чат по порядковому номеру из списка (1-based).
    Возвращает (успех, название чата).
    """
    chats = list_chats(vk_id)
    if not chats or index < 1 or index > len(chats):
        return False, ""
    chat = chats[index - 1]
    ok = switch_chat(vk_id, chat["id"])
    return ok, chat["name"]


def rename_chat(vk_id: int, new_name: str) -> bool:
    """Переименовывает активный чат. Возвращает True при успехе."""
    active = get_active_chat(vk_id)
    if not active:
        return False
    chat_id = active[0]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE chats SET name = ? WHERE id = ? AND vk_id = ?",
        (new_name[:50], chat_id, vk_id),
    )
    ok = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def delete_chat(vk_id: int, chat_id: int) -> tuple[bool, str]:
    """
    Удаляет чат пользователя.
    Если это был активный чат — переключает на предыдущий или создаёт новый.
    Возвращает (успех, сообщение).
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id FROM chats WHERE id = ? AND vk_id = ?", (chat_id, vk_id)
    )
    if not cursor.fetchone():
        conn.close()
        return False, "Чат не найден."

    # Проверяем, был ли это активный чат
    cursor.execute(
        "SELECT active_chat_id FROM user_state WHERE vk_id = ?", (vk_id,)
    )
    state = cursor.fetchone()
    was_active = state and state[0] == chat_id

    cursor.execute("DELETE FROM chats WHERE id = ? AND vk_id = ?", (chat_id, vk_id))
    conn.commit()
    conn.close()

    if was_active:
        # Переключаемся на любой оставшийся чат
        chats = list_chats(vk_id)
        if chats:
            switch_chat(vk_id, chats[-1]["id"])
            return True, f"Чат удалён. Активный чат: «{chats[-1]['name']}»."
        else:
            # Нет чатов — создадим новый
            create_chat(vk_id, "Чат 1")
            return True, "Чат удалён. Создан новый чат."

    return True, "Чат удалён."


def delete_active_chat(vk_id: int) -> tuple[bool, str]:
    """Удаляет текущий активный чат."""
    active = get_active_chat(vk_id)
    if not active:
        return False, "Нет активного чата."
    return delete_chat(vk_id, active[0])


def count_chats(vk_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM chats WHERE vk_id = ?", (vk_id,))
    count = cursor.fetchone()[0]
    conn.close()
    return count
