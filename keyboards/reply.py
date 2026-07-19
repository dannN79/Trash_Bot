from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu(role: str = "user") -> ReplyKeyboardMarkup:
    """
    Главное меню.
    Кнопка «🔧 Админ-панель» — только для moderator / superadmin.
    """
    buttons = [
        [KeyboardButton(text="📋 Мои публикации"), KeyboardButton(text="⚠️ Пожаловаться на пост")],
        [KeyboardButton(text="🔄 Сменить канал"), KeyboardButton(text="🔔 Уведомления")],
        [KeyboardButton(text="🆔 Сменить ID (250 ⭐️)"), KeyboardButton(text="📜 Правила")],
    ]
    if role in ("moderator", "superadmin"):
        buttons.append([KeyboardButton(text="🔧 Админ-панель")])

    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, is_persistent=True)


def posts_menu() -> ReplyKeyboardMarkup:
    """Меню раздела публикаций."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Новая публикация")],
            [KeyboardButton(text="🔙 Назад в главное меню")],
        ],
        resize_keyboard=True,
        is_persistent=True
    )


def back_only_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с одной кнопкой — используется при вводе."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Назад")]],
        resize_keyboard=True,
        is_persistent=True
    )


def draft_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура для режима создания публикации."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔙 Назад"), KeyboardButton(text="✅ Готово")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )