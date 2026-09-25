"""
VK-бот с подпиской, мультичатами и DeepSeek.

Что делает:
  1. Принимает сообщения из сообщества ВКонтакте через Callback API.
  2. Если у пользователя нет подписки — предлагает оплатить.
  3. Если подписка есть — отправляет вопрос в DeepSeek в контексте активного чата.
  4. Каждый пользователь может иметь до 20 независимых чатов с отдельной историей.
  5. Принимает уведомление об оплате от ЮКассы и активирует подписку.

Запуск: python bot.py
"""

import json
import os
import random
import logging

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

import database as db

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vkbot")

app = Flask(__name__)

# ────────────────────────────  НАСТРОЙКИ  ────────────────────────────
VK_TOKEN        = os.getenv("VK_TOKEN")
VK_CONFIRMATION = os.getenv("VK_CONFIRMATION")
VK_GROUP_ID     = os.getenv("VK_GROUP_ID", "")
VK_API_VERSION  = "5.199"

DEEPSEEK_KEY    = os.getenv("DEEPSEEK_KEY")
DEEPSEEK_MODEL  = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET  = os.getenv("YOOKASSA_SECRET")

PRICE_RUB  = os.getenv("PRICE_RUB", "499")
SUB_DAYS   = int(os.getenv("SUB_DAYS", "30"))
RETURN_URL = os.getenv("RETURN_URL", "https://vk.com")

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "Ты полезный и дружелюбный ассистент. Отвечай кратко и по делу на русском языке.",
)

MAX_HISTORY = 10  # последних сообщений на чат (пар user+assistant = 5 обменов)

# История диалогов в памяти: {(vk_id, chat_id): [{"role": ..., "content": ...}, ...]}
# При перезапуске сервера стирается. Для постоянного хранения нужна внешняя БД.
history: dict[tuple[int, int], list] = {}


# ────────────────────────────  VK HELPERS  ────────────────────────────

def vk_send(peer_id: int, text: str, keyboard: dict | None = None):
    """Отправляет сообщение пользователю от имени сообщества."""
    if len(text) > 4000:
        text = text[:4000] + "\n\n…(ответ обрезан)"
    payload = {
        "access_token": VK_TOKEN,
        "v": VK_API_VERSION,
        "peer_id": peer_id,
        "message": text,
        "random_id": random.getrandbits(31),
    }
    if keyboard:
        payload["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        r = requests.post(
            "https://api.vk.com/method/messages.send",
            data=payload,
            timeout=15,
        )
        data = r.json()
        if "error" in data:
            log.error("VK send error: %s", data["error"])
    except Exception as e:
        log.exception("Не удалось отправить сообщение: %s", e)


def make_main_keyboard() -> dict:
    """Клавиатура главного меню."""
    return {
        "one_time": False,
        "buttons": [
            [
                {"action": {"type": "text", "label": "💬 Новый чат"}, "color": "primary"},
                {"action": {"type": "text", "label": "📋 Мои чаты"}, "color": "secondary"},
            ],
            [
                {"action": {"type": "text", "label": "💳 Статус подписки"}, "color": "secondary"},
                {"action": {"type": "text", "label": "❓ Помощь"}, "color": "secondary"},
            ],
        ],
    }


# ────────────────────────────  DEEPSEEK  ────────────────────────────

def ask_deepseek(vk_id: int, chat_id: int, user_text: str) -> str:
    """Отправляет вопрос в DeepSeek в контексте конкретного чата."""
    key = (vk_id, chat_id)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += history.get(key, [])
    msgs.append({"role": "user", "content": user_text})

    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": msgs,
                "temperature": 0.7,
                "max_tokens": 1500,
            },
            timeout=90,
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.exception("DeepSeek error: %s", e)
        return "Извини, сейчас не могу ответить — техническая ошибка. Попробуй чуть позже."

    # Обновляем историю этого чата
    hist = history.setdefault(key, [])
    hist.append({"role": "user", "content": user_text})
    hist.append({"role": "assistant", "content": answer})
    del hist[:-MAX_HISTORY]

    return answer


# ────────────────────────────  ПЛАТЕЖИ  ────────────────────────────

def create_payment(vk_id: int) -> str | None:
    """Создаёт платёж в ЮКассе и возвращает ссылку на оплату."""
    payload = {
        "amount": {"value": f"{PRICE_RUB}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": RETURN_URL},
        "description": f"Подписка на бота, {SUB_DAYS} дней",
        "metadata": {"vk_id": str(vk_id)},
    }
    try:
        r = requests.post(
            "https://api.yookassa.ru/v3/payments",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET),
            headers={
                "Idempotence-Key": f"vk{vk_id}-{random.getrandbits(32)}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["confirmation"]["confirmation_url"]
    except Exception as e:
        log.exception("ЮКасса: не удалось создать платёж: %s", e)
        return None


def payment_is_paid(payment_id: str) -> bool:
    """Перепроверяет статус платежа напрямую в ЮКассе."""
    try:
        r = requests.get(
            f"https://api.yookassa.ru/v3/payments/{payment_id}",
            auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET),
            timeout=20,
        )
        r.raise_for_status()
        return r.json().get("status") == "paid"
    except Exception as e:
        log.exception("ЮКасса: не удалось проверить платёж: %s", e)
        return False


# ────────────────────────────  ОБРАБОТКА ЧАТОВ  ────────────────────────────

def format_chats_list(vk_id: int) -> str:
    """Возвращает текстовый список чатов пользователя."""
    chats = db.list_chats(vk_id)
    if not chats:
        return "У тебя пока нет чатов. Напиши «Новый чат» чтобы создать первый."

    lines = ["📋 Твои чаты:\n"]
    for c in chats:
        marker = "▶️" if c["is_active"] else "  "
        lines.append(f"{marker} {c['index']}. {c['name']} (создан {c['created_at']})")

    lines.append(
        "\nЧтобы переключиться — напиши: /switch 2\n"
        "Переименовать активный чат: /rename Моя тема\n"
        "Удалить активный чат: /deletechat"
    )
    return "\n".join(lines)


# ────────────────────────────  ГЛАВНАЯ ЛОГИКА  ────────────────────────────

def handle_message(vk_id: int, peer_id: int, text: str):
    """Маршрутизация входящего сообщения."""
    cmd = text.strip()
    cmd_lower = cmd.lower()

    # ── Приветствие ──────────────────────────────────────────
    if cmd_lower in ("/start", "начать", "привет", "здравствуйте"):
        if db.is_subscribed(vk_id):
            active = db.get_active_chat(vk_id)
            chat_name = active[1] if active else "Новый чат"
            vk_send(
                peer_id,
                f"👋 Привет! Подписка активна.\n\nАктивный чат: «{chat_name}»\n\nЗадай любой вопрос — отвечу!",
                keyboard=make_main_keyboard(),
            )
        else:
            vk_send(
                peer_id,
                f"👋 Привет!\n\nЧтобы пользоваться ботом, нужна подписка.\n\n"
                f"💰 Стоимость: {PRICE_RUB} ₽ за {SUB_DAYS} дней\n\n"
                "Напиши «Оплатить» — пришлю ссылку на оплату.",
            )
        return

    # ── Статус подписки ──────────────────────────────────────
    if cmd_lower in ("/status", "статус", "💳 статус подписки", "статус подписки"):
        if db.is_subscribed(vk_id):
            vk_send(peer_id, f"✅ Подписка активна до {db.get_expiry(vk_id)}.", keyboard=make_main_keyboard())
        else:
            vk_send(peer_id, "❌ Активной подписки нет.\n\nНапиши «Оплатить» чтобы оформить.")
        return

    # ── Оплата ───────────────────────────────────────────────
    if cmd_lower in ("/subscribe", "оплатить", "подписка", "💳 оплатить"):
        if db.is_subscribed(vk_id):
            vk_send(peer_id, "У тебя уже есть активная подписка 😉", keyboard=make_main_keyboard())
            return
        vk_send(peer_id, "Создаю ссылку на оплату…")
        url = create_payment(vk_id)
        if url:
            vk_send(peer_id, f"👉 Оплатить: {url}\n\nПосле оплаты напиши /status.")
        else:
            vk_send(peer_id, "Не удалось создать платёж. Попробуй позже.")
        return

    # ── Создать новый чат ────────────────────────────────────
    if cmd_lower in ("/newchat", "новый чат", "💬 новый чат"):
        if not db.is_subscribed(vk_id):
            vk_send(peer_id, "Сначала оформи подписку. Напиши «Оплатить».")
            return
        count = db.count_chats(vk_id)
        chat_name = f"Чат {count + 1}"
        chat_id = db.create_chat(vk_id, chat_name)
        if chat_id is None:
            vk_send(peer_id, f"Достигнут лимит в {db.MAX_CHATS_PER_USER} чатов. Удали ненужные через /deletechat.")
        else:
            vk_send(
                peer_id,
                f"✅ Создан новый чат «{chat_name}».\n\n"
                "История чистая — можем начинать новую тему!\n\n"
                "Переименовать чат: /rename Название темы",
                keyboard=make_main_keyboard(),
            )
        return

    # ── Список чатов ─────────────────────────────────────────
    if cmd_lower in ("/chats", "мои чаты", "📋 мои чаты", "чаты"):
        if not db.is_subscribed(vk_id):
            vk_send(peer_id, "Сначала оформи подписку. Напиши «Оплатить».")
            return
        vk_send(peer_id, format_chats_list(vk_id), keyboard=make_main_keyboard())
        return

    # ── Переключить чат: /switch 2 ───────────────────────────
    if cmd_lower.startswith("/switch ") or cmd_lower.startswith("switch "):
        if not db.is_subscribed(vk_id):
            vk_send(peer_id, "Сначала оформи подписку.")
            return
        parts = cmd.split(None, 1)
        if len(parts) < 2 or not parts[1].isdigit():
            vk_send(peer_id, "Используй формат: /switch 2 (где 2 — номер чата из списка)")
            return
        idx = int(parts[1])
        ok, name = db.switch_chat_by_index(vk_id, idx)
        if ok:
            vk_send(
                peer_id,
                f"✅ Переключился на чат «{name}».\n\nПродолжаем с того места, где остановились.",
                keyboard=make_main_keyboard(),
            )
        else:
            vk_send(peer_id, f"Чат с номером {idx} не найден. Посмотри список: /chats")
        return

    # ── Переименовать активный чат: /rename Новое название ───
    if cmd_lower.startswith("/rename ") or cmd_lower.startswith("rename "):
        if not db.is_subscribed(vk_id):
            vk_send(peer_id, "Сначала оформи подписку.")
            return
        parts = cmd.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            vk_send(peer_id, "Используй формат: /rename Название темы")
            return
        new_name = parts[1].strip()
        ok = db.rename_chat(vk_id, new_name)
        if ok:
            vk_send(peer_id, f"✅ Чат переименован в «{new_name}».", keyboard=make_main_keyboard())
        else:
            vk_send(peer_id, "Не удалось переименовать. Попробуй ещё раз.")
        return

    # ── Удалить активный чат ─────────────────────────────────
    if cmd_lower in ("/deletechat", "deletechat", "удалить чат"):
        if not db.is_subscribed(vk_id):
            vk_send(peer_id, "Сначала оформи подписку.")
            return
        ok, msg = db.delete_active_chat(vk_id)
        # Чистим историю удалённого чата из памяти
        active = db.get_active_chat(vk_id)
        if ok and active:
            # Удалённый чат уже не активный — убираем только его ключи
            keys_to_del = [k for k in history if k[0] == vk_id]
            # Оставляем только активный чат
            for k in keys_to_del:
                if k[1] != active[0]:
                    history.pop(k, None)
        vk_send(peer_id, f"🗑 {msg}", keyboard=make_main_keyboard())
        return

    # ── Помощь ───────────────────────────────────────────────
    if cmd_lower in ("/help", "помощь", "❓ помощь", "help"):
        vk_send(
            peer_id,
            "Команды:\n\n"
            "💬 Новый чат — начать разговор на новую тему\n"
            "📋 Мои чаты — посмотреть все чаты\n"
            "/switch 2 — переключиться на чат №2\n"
            "/rename Название — переименовать активный чат\n"
            "/deletechat — удалить активный чат\n\n"
            "💳 Статус подписки — посмотреть дату окончания\n"
            "Оплатить — оформить подписку\n\n"
            "Любой другой текст — вопрос к AI (при активной подписке)",
            keyboard=make_main_keyboard(),
        )
        return

    # ── Проверка подписки перед AI-запросом ──────────────────
    if not db.is_subscribed(vk_id):
        vk_send(
            peer_id,
            f"👋 Чтобы задавать вопросы боту, нужна подписка.\n\n"
            f"💰 Стоимость: {PRICE_RUB} ₽ за {SUB_DAYS} дней\n\n"
            "Напиши «Оплатить» — пришлю ссылку.",
        )
        return

    # ── AI-ответ в контексте активного чата ──────────────────
    active = db.get_active_chat(vk_id)
    if not active:
        vk_send(peer_id, "Не удалось определить активный чат. Напиши «Новый чат».")
        return

    chat_id, chat_name = active
    answer = ask_deepseek(vk_id, chat_id, cmd)
    # Показываем название активного чата только если у пользователя больше одного чата
    if db.count_chats(vk_id) > 1:
        header = f"[{chat_name}]\n\n"
    else:
        header = ""
    vk_send(peer_id, header + answer)


# ────────────────────────────  VK CALLBACK API  ────────────────────────────

@app.route("/vk", methods=["POST"])
def vk_callback():
    data = request.get_json(silent=True) or {}
    event_type = data.get("type")

    if event_type == "confirmation":
        return VK_CONFIRMATION or ""

    if event_type == "message_new":
        obj = data.get("object", {}).get("message", {})
        vk_id = obj.get("from_id")
        peer_id = obj.get("peer_id", vk_id)
        text = (obj.get("text") or "").strip()

        if not vk_id or vk_id < 0:
            return "ok"

        handle_message(vk_id, peer_id, text)

    return "ok"


# ────────────────────────────  ВЕБХУК ОПЛАТЫ  ────────────────────────────

@app.route("/payment/webhook", methods=["POST"])
def payment_webhook():
    data = request.get_json(silent=True) or {}
    log.info("Вебхук оплаты: %s", data.get("event"))

    if data.get("event") != "payment.succeeded":
        return jsonify(ok=True)

    obj = data.get("object", {})
    payment_id = obj.get("id")
    vk_id_raw = (obj.get("metadata") or {}).get("vk_id")

    if not payment_id or not vk_id_raw:
        log.warning("В вебхуке нет payment_id или vk_id")
        return jsonify(ok=True)

    if not payment_is_paid(payment_id):
        log.warning("Платёж %s не подтверждён", payment_id)
        return jsonify(ok=True)

    vk_id = int(vk_id_raw)
    db.add_or_renew_subscription(vk_id, payment_id, days=SUB_DAYS)
    log.info("Подписка активирована: vk_id=%s до %s", vk_id, db.get_expiry(vk_id))

    # Создаём первый чат автоматически при первой оплате
    if db.count_chats(vk_id) == 0:
        db.create_chat(vk_id, "Чат 1")

    vk_send(
        vk_id,
        f"🎉 Оплата получена! Подписка активна до {db.get_expiry(vk_id)}.\n\n"
        "Задай любой вопрос — отвечу!",
        keyboard=make_main_keyboard(),
    )
    return jsonify(ok=True)


# ────────────────────────────  ПРОВЕРКА ЖИВОСТИ  ────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    db.init_db()
    port = int(os.getenv("PORT", "8080"))
    log.info("Бот запущен на порту %s", port)
    app.run(host="0.0.0.0", port=port)
