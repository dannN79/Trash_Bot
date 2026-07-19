"""
utils.py — вспомогательные функции.
"""

import asyncio
import logging
from aiogram import Bot
from aiogram.types import Message, InlineKeyboardMarkup
from aiogram.exceptions import AiogramError
from aiogram.fsm.context import FSMContext
from zoneinfo import ZoneInfo
from datetime import timezone as pytimezone, datetime
from sqlalchemy import select

from db.models import Channel, Ban

log = logging.getLogger(__name__)

# Ключи сообщений, которые отслеживаются для очистки
_TRACKED_MSG_KEYS = (
    "main_menu_msg_id",
    "posts_list_msg_id",
    "posts_kb_msg_id",
    "post_card_msg_id",
    "forwarded_msg_id",
    "prompt_msg_id",
    "channels_msg_id",
    "admin_menu_msg_id",
    "admin_sub_msg_id",
    "admin_post_card_msg_id",
    "admin_fwd_msg_id",
    "admin_kb_msg_id",
    "notification_panel_msg_id",
    "notification_sub_msg_id",
)


# ──────────────────────────────────────────────
# Базовые операции
# ──────────────────────────────────────────────

async def safe_delete(bot: Bot, chat_id: int, message_id: int | list[int] | None) -> None:
    """Удаляет сообщение(я), не падая если они уже удалены."""
    if not message_id:
        return
    if isinstance(message_id, list):
        for m_id in message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=m_id)
            except Exception:
                pass
    else:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass


async def cleanup_chat(bot: Bot, chat_id: int, state: FSMContext, keep: tuple = ()) -> None:
    """
    Удаляет ВСЕ отслеживаемые сообщения бота из чата.
    `keep` — кортеж ключей, которые НЕ надо удалять.
    """
    data = await state.get_data()
    reset = {}
    for key in _TRACKED_MSG_KEYS:
        msg_id = data.get(key)
        if msg_id and key not in keep:
            await safe_delete(bot, chat_id, msg_id)
            reset[key] = None
    if reset:
        await state.update_data(**reset)


async def _delayed_delete(bot: Bot, chat_id: int, message_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    await safe_delete(bot, chat_id, message_id)


# ──────────────────────────────────────────────
# Авто-удаляемые уведомления
# ──────────────────────────────────────────────

async def notify(
    message: Message,
    text: str,
    delay: float = 3.5,
    parse_mode: str = "HTML",
) -> Message:
    sent = await message.answer(text, parse_mode=parse_mode)
    asyncio.create_task(_delayed_delete(message.bot, message.chat.id, sent.message_id, delay))
    return sent


# ──────────────────────────────────────────────
# Удаление пары «подсказка бота + ответ пользователя»
# ──────────────────────────────────────────────

async def delete_pair(user_message: Message, prompt_msg_id: int | None) -> None:
    bot = user_message.bot
    chat_id = user_message.chat.id
    await safe_delete(bot, chat_id, user_message.message_id)
    if prompt_msg_id:
        await safe_delete(bot, chat_id, prompt_msg_id)


# ──────────────────────────────────────────────
# Редактирование / переотправка инлайн-сообщения
# ──────────────────────────────────────────────

async def edit_or_resend(
    bot: Bot,
    chat_id: int,
    old_message_id: int | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str = "HTML",
) -> int:
    if old_message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=old_message_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return old_message_id
        except AiogramError:
            await safe_delete(bot, chat_id, old_message_id)

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    return sent.message_id


# ──────────────────────────────────────────────
# Вспомогательные функции часового пояса и бана
# ──────────────────────────────────────────────

def utc_to_local(utc_dt: datetime, tz_name: str) -> datetime | None:
    """Конвертирует datetime из UTC в локальный часовой пояс."""
    if not utc_dt:
        return None
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=pytimezone.utc)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    return utc_dt.astimezone(tz)


def utcnow() -> datetime:
    """Текущее UTC-время без tzinfo (для SQLite)."""
    return datetime.now(pytimezone.utc).replace(tzinfo=None)


async def get_channel_timezone(session, channel_id: int) -> str:
    if not channel_id:
        return "Europe/Moscow"
    result = await session.execute(
        select(Channel.timezone).where(Channel.channel_id == channel_id)
    )
    val = result.scalar_one_or_none()
    return val if val else "Europe/Moscow"


async def get_channel_title(session, channel_id: int) -> str:
    result = await session.execute(
        select(Channel.title).where(Channel.channel_id == channel_id)
    )
    return result.scalar_one_or_none() or "неизвестный канал"


async def get_user_role(session, user_id: int, channel_id: int) -> str:
    from db.models import ChannelUser
    result = await session.execute(
        select(ChannelUser.role).where(
            ChannelUser.user_id == user_id,
            ChannelUser.channel_id == channel_id,
        )
    )
    return result.scalar_one_or_none() or "user"


async def is_user_banned(session, user_id: int, channel_id: int) -> tuple[bool, datetime | None, str | None]:
    now = utcnow()
    result = await session.execute(
        select(Ban)
        .where(
            Ban.channel_id == channel_id,
            Ban.user_id == user_id,
            Ban.ban_until > now
        )
        .order_by(Ban.ban_until.desc())
    )
    ban = result.scalars().first()
    if ban:
        return True, ban.ban_until, ban.reason
    return False, None, None