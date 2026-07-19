import re
import json
from datetime import datetime, timedelta, timezone as pytimezone
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import AiogramError
from sqlalchemy import select, func, update

from db.database import AsyncSessionLocal
from db.models import User, Channel, ChannelUser, Post, Ban, Notification
from states.states import AdminStates
from utils import (
    safe_delete, notify, delete_pair, edit_or_resend, cleanup_chat,
    utc_to_local, get_channel_timezone, get_channel_title,
    get_user_role, utcnow,
)
from keyboards.reply import main_menu, back_only_keyboard

router = Router()

PAGE_SIZE = 10


# ══════════════════════════════════════════════════════════
# Проверка прав
# ══════════════════════════════════════════════════════════

async def check_admin_rights(session, user_id: int, channel_id: int) -> str | None:
    result = await session.execute(
        select(ChannelUser.role).where(
            ChannelUser.user_id == user_id,
            ChannelUser.channel_id == channel_id
        )
    )
    role = result.scalar_one_or_none()
    return role if role in ("moderator", "superadmin") else None


# ══════════════════════════════════════════════════════════
# Главное меню админ-панели
# ══════════════════════════════════════════════════════════

async def enter_admin_panel(bot, chat_id: int, user_id: int, channel_id: int, state: FSMContext):
    await cleanup_chat(bot, chat_id, state)

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, user_id, channel_id)
        if not role:
            await bot.send_message(chat_id, "❌ Доступ запрещен.")
            return

        channel_result = await session.execute(
            select(Channel).where(Channel.channel_id == channel_id)
        )
        channel = channel_result.scalar_one_or_none()
        if not channel:
            await bot.send_message(chat_id, "Канал не найден.")
            return

        tz_name = channel.timezone or "Europe/Moscow"

        users_count = await session.scalar(
            select(func.count(ChannelUser.user_id)).where(
                ChannelUser.channel_id == channel_id
            )
        ) or 0

        mods_count = await session.scalar(
            select(func.count(ChannelUser.user_id)).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.role == "moderator"
            )
        ) or 0

        posts_count = await session.scalar(
            select(func.count(Post.id)).where(
                Post.channel_id == channel_id,
                Post.is_deleted == False
            )
        ) or 0

        bans_count = await session.scalar(
            select(func.count(Ban.id)).where(
                Ban.channel_id == channel_id,
                Ban.ban_until > utcnow()
            )
        ) or 0

    text = (
        f"🔧 <b>Панель администратора</b>\n\n"
        f"📢 Канал: <b>{channel.title}</b>\n"
        f"🕒 Часовой пояс: <code>{tz_name}</code>\n\n"
        f"<b>📊 Статистика:</b>\n"
        f"👤 Участников: <code>{users_count}</code>\n"
        f"🛡 Модераторов: <code>{mods_count}</code>\n"
        f"📄 Постов: <code>{posts_count}</code>\n"
        f"🚫 Банов: <code>{bans_count}</code>"
    )

    buttons = []
    if role == "superadmin":
        buttons.append([
            InlineKeyboardButton(text="👥 Модераторы", callback_data="adm_mods")
        ])
        buttons.append([
            InlineKeyboardButton(text="📢 Рассылка / Уведомления", callback_data="adm_notify_menu")
        ])
    buttons.append([
        InlineKeyboardButton(text="📄 Все публикации", callback_data="adm_posts:0")
    ])
    buttons.append([
        InlineKeyboardButton(text="🚫 Управление банами", callback_data="adm_bans")
    ])
    if role == "superadmin":
        buttons.append([
            InlineKeyboardButton(text="🕒 Часовой пояс", callback_data="adm_tz")
        ])
    buttons.append([
        InlineKeyboardButton(text="❌ Выйти из панели", callback_data="adm_exit")
    ])

    # Восстанавливаем главное меню внизу экрана
    sent_kb = await bot.send_message(
        chat_id=chat_id,
        text="🛡 <i>Открыта панель управления:</i>",
        reply_markup=main_menu(role),
        parse_mode="HTML"
    )

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(
        admin_menu_msg_id=sent.message_id,
        admin_sub_msg_id=None,
        admin_kb_msg_id=sent_kb.message_id
    )


@router.callback_query(F.data == "adm_exit")
async def process_admin_exit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    from handlers.start import show_main_menu_msg
    await show_main_menu_msg(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, state
    )


@router.callback_query(F.data == "adm_back")
async def process_admin_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    # Очищаем временные данные
    await state.update_data(
        ban_target_user_id=None,
        ban_duration_days=None,
        admin_delete_target_post_id=None,
        escalate_post_id=None,
        escalate_owner_id=None,
    )

    for key in ("admin_post_card_msg_id", "admin_fwd_msg_id", "admin_sub_msg_id"):
        if data.get(key):
            await safe_delete(callback.bot, callback.message.chat.id, data[key])

    await state.update_data(
        admin_post_card_msg_id=None, admin_fwd_msg_id=None, admin_sub_msg_id=None
    )
    await callback.answer()
    await enter_admin_panel(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state
    )


# ══════════════════════════════════════════════════════════
# Часовой пояс
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_tz")
async def process_admin_tz(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    if not channel_id:
        await callback.answer("Канал не выбран.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("🕒 Смена часового пояса доступна только владельцу канала.", show_alert=True)
            return

    zones = [
        ("UTC (GMT+0)", "UTC"),
        ("Москва (UTC+3)", "Europe/Moscow"),
        ("Киев (UTC+3)", "Europe/Kyiv"),
        ("Минск (UTC+3)", "Europe/Minsk"),
        ("Берлин (UTC+2)", "Europe/Berlin"),
        ("Екатеринбург (UTC+5)", "Asia/Yekaterinburg"),
        ("Новосибирск (UTC+7)", "Asia/Novosibirsk"),
        ("Владивосток (UTC+10)", "Asia/Vladivostok"),
    ]

    buttons = []
    for label, tz in zones:
        buttons.append([InlineKeyboardButton(
            text=label, callback_data=f"set_tz:{tz}"
        )])
    buttons.append([InlineKeyboardButton(text="✏️ Ввести свой", callback_data="custom_tz")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])

    sent = await callback.message.answer(
        "🕒 <b>Часовой пояс канала</b>\n\nВыберите:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(admin_sub_msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("set_tz:"))
async def process_set_tz(callback: CallbackQuery, state: FSMContext):
    tz_name = callback.data.split("set_tz:")[1]
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Нет прав.", show_alert=True)
            return
        await session.execute(
            update(Channel).where(Channel.channel_id == channel_id).values(timezone=tz_name)
        )
        await session.commit()

    await callback.answer(f"✅ Пояс: {tz_name}")
    await enter_admin_panel(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state
    )


@router.callback_query(F.data == "custom_tz")
async def process_custom_tz(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Нет прав.", show_alert=True)
            return

    await state.set_state(AdminStates.waiting_custom_timezone)
    prompt = await callback.message.answer(
        "✏️ Введите часовой пояс в формате IANA\n"
        "(например, <code>America/New_York</code>):",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_custom_timezone)
async def save_custom_tz(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    tz_name = message.text.strip()

    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(tz_name)
    except Exception:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Неверный часовой пояс. Попробуйте ещё.")
        return

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, message.from_user.id, channel_id)
        if role != "superadmin":
            await notify(message, "❌ У вас нет прав на изменение часового пояса.")
            await state.set_state(None)
            return

        await session.execute(
            update(Channel).where(Channel.channel_id == channel_id).values(timezone=tz_name)
        )
        await session.commit()

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await notify(message, f"✅ Пояс: {tz_name}!")
    await enter_admin_panel(message.bot, message.chat.id, message.from_user.id, channel_id, state)


# ══════════════════════════════════════════════════════════
# Управление модераторами
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_mods")
async def process_admin_mods(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Только владелец.", show_alert=True)
            return

        result = await session.execute(
            select(ChannelUser, User.username, User.full_name)
            .join(User, User.user_id == ChannelUser.user_id)
            .where(ChannelUser.channel_id == channel_id, ChannelUser.role == "moderator")
        )
        mods = result.all()

    buttons = []
    for cu, username, name in mods:
        name_str = f"@{username}" if username else name
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {name_str} (ID: {cu.internal_id})",
            callback_data=f"demote:{cu.user_id}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Назначить модератора", callback_data="promote_mod")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])

    sent = await callback.message.answer(
        "🛡 <b>Модераторы канала</b>\n\n"
        "Нажмите для снятия, или добавьте нового:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(admin_sub_msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "promote_mod")
async def process_promote_mod(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_moderator_internal_id)
    prompt = await callback.message.answer(
        "👤 Введите 9-значный ID пользователя\n"
        "(например, <code>000000002</code>):",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_moderator_internal_id)
async def save_moderator_promotion(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    internal_id = message.text.strip()

    async with AsyncSessionLocal() as session:
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.internal_id == internal_id
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu:
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(message, "⚠️ Пользователь не найден.")
            return

        if cu.role == "moderator":
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(message, "ℹ️ Уже модератор.")
            await state.set_state(None)
            return

        if cu.role == "superadmin":
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(message, "⚠️ Нельзя назначить владельца.")
            await state.set_state(None)
            return

        cu.role = "moderator"
        ch_title = await get_channel_title(session, channel_id)
        await session.commit()

        try:
            await message.bot.send_message(
                chat_id=cu.user_id,
                text=f"🎉 <b>Вам выдана роль модератора</b> в канале <b>{ch_title}</b>!",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await notify(message, "✅ Модератор назначен!")
    await enter_admin_panel(message.bot, message.chat.id, message.from_user.id, channel_id, state)


@router.callback_query(F.data.startswith("demote:"))
async def process_demote_mod(callback: CallbackQuery, state: FSMContext):
    target_user_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Нет прав.", show_alert=True)
            return

        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.user_id == target_user_id
            )
        )
        cu = cu_result.scalar_one_or_none()
        if cu and cu.role == "moderator":
            cu.role = "user"
            ch_title = await get_channel_title(session, channel_id)
            await session.commit()
            await callback.answer("Модератор разжалован.")
            try:
                await callback.bot.send_message(
                    chat_id=target_user_id,
                    text=f"ℹ️ <b>Ваша роль модератора снята</b> в канале <b>{ch_title}</b>.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            await callback.answer("Ошибка.", show_alert=True)

    await enter_admin_panel(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state
    )


# ══════════════════════════════════════════════════════════
# Управление банами
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_bans")
async def process_admin_bans(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if not role:
            await callback.answer("Нет прав.", show_alert=True)
            return

        result = await session.execute(
            select(Ban, User.username, User.full_name)
            .join(User, User.user_id == Ban.user_id)
            .where(Ban.channel_id == channel_id, Ban.ban_until > utcnow())
        )
        active_bans = result.all()
        tz_name = await get_channel_timezone(session, channel_id)

    buttons = []
    for ban, username, name in active_bans:
        name_str = f"@{username}" if username else name
        local_until = utc_to_local(ban.ban_until, tz_name)
        date_str = local_until.strftime("%d.%m %H:%M")
        buttons.append([InlineKeyboardButton(
            text=f"🔓 {name_str} (до {date_str})",
            callback_data=f"unban:{ban.id}"
        )])

    buttons.append([InlineKeyboardButton(text="➕ Забанить", callback_data="ban_flow")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])

    sent = await callback.message.answer(
        "🚫 <b>Баны канала</b>\n\nНажмите для разбана или забаньте нового:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(admin_sub_msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("unban:"))
async def process_unban(callback: CallbackQuery, state: FSMContext):
    ban_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if not role:
            await callback.answer("Нет прав.", show_alert=True)
            return

        result = await session.execute(select(Ban).where(Ban.id == ban_id))
        ban = result.scalar_one_or_none()
        if ban:
            ban.ban_until = utcnow()
            cu_result = await session.execute(
                select(ChannelUser).where(
                    ChannelUser.channel_id == channel_id,
                    ChannelUser.user_id == ban.user_id
                )
            )
            cu = cu_result.scalar_one_or_none()
            if cu:
                cu.deleted_by_admin_count = 0
            channel_title = await get_channel_title(session, channel_id)
            await session.commit()
            await callback.answer("✅ Разбанен!")
            try:
                await callback.bot.send_message(
                    chat_id=ban.user_id,
                    text=f"🔓 <b>Вы разбанены в канале «{channel_title}»!</b>",
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            await callback.answer("Бан не найден.", show_alert=True)

    await enter_admin_panel(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state
    )


@router.callback_query(F.data == "ban_flow")
async def process_ban_flow(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_ban_user_internal_id)
    prompt = await callback.message.answer(
        "👤 Введите 9-значный ID пользователя для бана:",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_ban_user_internal_id)
async def process_ban_user_id(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    internal_id = message.text.strip()

    async with AsyncSessionLocal() as session:
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.internal_id == internal_id
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu:
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(message, "⚠️ Пользователь не найден.")
            return
        if cu.role == "superadmin":
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(message, "⚠️ Нельзя забанить владельца.")
            await state.set_state(None)
            return

        await state.update_data(ban_target_user_id=cu.user_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 день", callback_data="ban_dur:1"),
            InlineKeyboardButton(text="7 дней", callback_data="ban_dur:7")
        ],
        [
            InlineKeyboardButton(text="14 дней", callback_data="ban_dur:14"),
            InlineKeyboardButton(text="Навсегда", callback_data="ban_dur:36500")
        ],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]
    ])

    await delete_pair(message, data.get("prompt_msg_id"))
    prompt = await message.answer(
        f"📅 Длительность бана для <code>{internal_id}</code>:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await state.set_state(AdminStates.waiting_ban_duration)


@router.callback_query(F.data.startswith("ban_dur:"))
async def process_ban_duration(callback: CallbackQuery, state: FSMContext):
    days = int(callback.data.split(":")[1])
    await state.update_data(ban_duration_days=days)

    data = await state.get_data()
    if data.get("prompt_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["prompt_msg_id"])

    await state.set_state(AdminStates.waiting_ban_reason)
    prompt = await callback.message.answer(
        "📝 Введите причину бана:",
        reply_markup=back_only_keyboard()
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_ban_reason)
async def process_ban_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    target_user_id = data.get("ban_target_user_id")
    days = data.get("ban_duration_days")

    # Проверяем: это эскалация модератора?
    escalate_owner_id = data.get("escalate_owner_id")
    if escalate_owner_id:
        await _handle_escalation(message, state)
        return

    reason = message.text.strip() if message.text else ""
    if not reason:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Причина не может быть пустой.")
        return

    ban_until = utcnow() + timedelta(days=days)

    async with AsyncSessionLocal() as session:
        session.add(Ban(
            channel_id=channel_id,
            user_id=target_user_id,
            banned_by=message.from_user.id,
            ban_until=ban_until,
            reason=reason
        ))
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.user_id == target_user_id
            )
        )
        cu = cu_result.scalar_one_or_none()
        if cu:
            cu.deleted_by_admin_count = 0
        await session.commit()

        try:
            tz_name = await get_channel_timezone(session, channel_id)
            ch_title = await get_channel_title(session, channel_id)
            local_until = utc_to_local(ban_until, tz_name)
            date_str = local_until.strftime("%d.%m.%Y %H:%M")
            await message.bot.send_message(
                chat_id=target_user_id,
                text=f"🚫 <b>Вы забанены в канале {ch_title}</b>\n"
                     f"До: <code>{date_str}</code>\n"
                     f"Причина: <i>{reason}</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await notify(message, "✅ Пользователь забанен!")
    await enter_admin_panel(message.bot, message.chat.id, message.from_user.id, channel_id, state)


# ══════════════════════════════════════════════════════════
# Все публикации (браузер постов)
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("adm_posts:"))
async def process_admin_browse_posts(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if not role:
            await callback.answer("Нет прав.", show_alert=True)
            return

        tz_name = await get_channel_timezone(session, channel_id)

        total = await session.scalar(
            select(func.count(Post.id)).where(
                Post.channel_id == channel_id, Post.is_deleted == False
            )
        ) or 0

        result = await session.execute(
            select(Post)
            .where(Post.channel_id == channel_id, Post.is_deleted == False)
            .order_by(Post.created_at.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        posts = result.scalars().all()

    if total == 0:
        await callback.answer("Нет публикаций.", show_alert=True)
        return

    rows = []
    for post in posts:
        local_dt = utc_to_local(post.created_at, tz_name)
        date_str = local_dt.strftime("%d.%m.%Y %H:%M")
        media_icons = {"photo": "🖼", "video": "🎬", "document": "📎", "animation": "🎞", "text": "📝", "album": "🖼"}
        icon = media_icons.get(post.media_type, "📝")
        preview = ""
        if post.text:
            preview = post.text[:30].replace("\n", " ")
            if len(post.text) > 30:
                preview += "…"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {date_str}  {preview}",
            callback_data=f"adm_vp:{post.id}"
        )])

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_posts:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_posts:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")])

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])

    sent = await callback.message.answer(
        f"📄 <b>Все публикации</b>\nВсего: {total} | Стр. {page + 1}/{total_pages}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )
    await state.update_data(admin_sub_msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_vp:"))
async def process_admin_view_post(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if not role:
            await callback.answer("Нет прав.", show_alert=True)
            return

        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await callback.answer("Пост удален.", show_alert=True)
            return

        cu_result = await session.execute(
            select(ChannelUser.internal_id).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.user_id == post.user_id
            )
        )
        author_id = cu_result.scalar_one_or_none() or "—"
        tz_name = await get_channel_timezone(session, post.channel_id)

    # Чистим старую карточку
    if data.get("admin_post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_post_card_msg_id"])
    if data.get("admin_fwd_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_fwd_msg_id"])

    # Пересылаем
    import json
    admin_fwd_msg_id = None
    message_ids = []
    if post.media_type == "album" and post.file_ids:
        try:
            album_data = json.loads(post.file_ids)
            if isinstance(album_data, dict):
                message_ids = album_data.get("message_ids", [])
        except Exception:
            pass
    if not message_ids:
        message_ids = [post.message_id]

    try:
        fwd_ids = []
        for msg_id in message_ids:
            fwd = await callback.bot.forward_message(
                chat_id=callback.message.chat.id,
                from_chat_id=post.channel_id,
                message_id=msg_id
            )
            fwd_ids.append(fwd.message_id)
        admin_fwd_msg_id = fwd_ids if len(fwd_ids) > 1 else (fwd_ids[0] if fwd_ids else None)
    except Exception:
        pass

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_del:{post.id}"),
            InlineKeyboardButton(
                text="🚫 Забанить автора",
                callback_data=f"adm_ban:{post.user_id}:{channel_id}"
            )
        ],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="adm_close_pv")]
    ])

    local_dt = utc_to_local(post.created_at, tz_name)
    dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

    sent = await callback.message.answer(
        f"📄 <b>Публикация от {dt_str}</b>\n"
        f"👤 Автор: <code>{author_id}</code>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await state.update_data(
        admin_post_card_msg_id=sent.message_id,
        admin_fwd_msg_id=admin_fwd_msg_id
    )
    await callback.answer()


@router.callback_query(F.data == "adm_close_pv")
async def process_admin_close_pv(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("admin_post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_post_card_msg_id"])
    if data.get("admin_fwd_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_fwd_msg_id"])
    await state.update_data(admin_post_card_msg_id=None, admin_fwd_msg_id=None)
    await callback.answer()


# ══════════════════════════════════════════════════════════
# Модераторские действия (Удаление / Бан из репорта)
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("adm_del:"))
async def process_admin_del_post(callback: CallbackQuery, state: FSMContext):
    post_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    # Берём channel_id из поста, если в state нет
    async with AsyncSessionLocal() as session:
        post_result = await session.execute(select(Post).where(Post.id == post_id))
        post = post_result.scalar_one_or_none()
        if post:
            channel_id = post.channel_id

        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if not role:
            await callback.answer("Нет прав.", show_alert=True)
            return

    await state.update_data(
        admin_delete_target_post_id=post_id,
        active_channel_id=channel_id
    )
    await state.set_state(AdminStates.waiting_post_delete_reason)

    prompt = await callback.message.answer(
        "📝 Причина удаления публикации:",
        reply_markup=back_only_keyboard()
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_post_delete_reason)
async def process_admin_delete_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    post_id = data.get("admin_delete_target_post_id")
    reason = message.text.strip() if message.text else ""

    if not reason:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Причина обязательна.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, "Пост не найден.")
            await state.set_state(None)
            return

        message_ids = []
        if post.media_type == "album" and post.file_ids:
            try:
                album_data = json.loads(post.file_ids)
                if isinstance(album_data, dict):
                    message_ids = album_data.get("message_ids", [])
            except Exception:
                pass
        if not message_ids:
            message_ids = [post.message_id]

        for msg_id in message_ids:
            try:
                await message.bot.delete_message(
                    chat_id=post.channel_id, message_id=msg_id
                )
            except Exception:
                pass

        post.is_deleted = True
        post.deleted_by = message.from_user.id
        post.deleted_at = utcnow()
        post.delete_reason = f"Модератор: {reason}"

        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == post.user_id,
                ChannelUser.channel_id == post.channel_id
            )
        )
        cu = cu_result.scalar_one_or_none()
        if cu:
            if cu.role != "superadmin":
                cu.deleted_by_admin_count = (cu.deleted_by_admin_count or 0) + 1
            tz_name = await get_channel_timezone(session, post.channel_id)
            local_created = utc_to_local(post.created_at, tz_name)
            created_str = local_created.strftime("%d.%m.%Y %H:%M") if local_created else "—"

            if cu.role != "superadmin" and cu.deleted_by_admin_count >= 3:
                ban_until = utcnow() + timedelta(days=14)
                session.add(Ban(
                    channel_id=post.channel_id,
                    user_id=post.user_id,
                    banned_by=message.from_user.id,
                    ban_until=ban_until,
                    reason=f"Автобан: 3 нарушения. Причина: {reason}"
                ))
                local_until = utc_to_local(ban_until, tz_name)
                date_str = local_until.strftime("%d.%m.%Y %H:%M")
                ntf_text = f"🚫 <b>Автобан на 14 дней (до {date_str})</b> за 3 нарушения.\nУдалена публикация от {created_str}.\nПричина последнего удаления: <i>{reason}</i>"
            else:
                if cu.role == "superadmin":
                    ntf_text = f"⚠️ <b>Ваша публикация от {created_str} была удалена модератором.</b>\nПричина: <i>{reason}</i>"
                else:
                    ntf_text = f"⚠️ <b>Ваша публикация от {created_str} была удалена модератором.</b>\nПричина: <i>{reason}</i>\nНарушений: {cu.deleted_by_admin_count}/3"
            
            ntf = Notification(
                sender_id=message.from_user.id,
                receiver_id=post.user_id,
                channel_id=post.channel_id,
                text=ntf_text,
                is_read=False,
                created_at=utcnow()
            )
            session.add(ntf)
            await session.flush()
            
            try:
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📖 Прочитать", callback_data=f"ntf_read_alert:{ntf.id}")]
                ])
                await message.bot.send_message(
                    chat_id=post.user_id,
                    text="🔔 <b>У вас новое уведомление!</b>",
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
            except Exception:
                pass

        await session.commit()

    # Чистка
    for key in ("admin_post_card_msg_id", "admin_fwd_msg_id"):
        if data.get(key):
            await safe_delete(message.bot, message.chat.id, data[key])

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await state.update_data(
        admin_delete_target_post_id=None,
        admin_post_card_msg_id=None,
        admin_fwd_msg_id=None,
        prompt_msg_id=None
    )

    await notify(message, "✅ Пост удалён.")
    await enter_admin_panel(
        message.bot, message.chat.id,
        message.from_user.id, channel_id, state
    )


@router.callback_query(F.data.startswith("adm_ban:"))
async def process_admin_ban_callback(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    target_user_id = int(parts[1])
    channel_id = int(parts[2])

    async with AsyncSessionLocal() as session:
        target_role = await get_user_role(session, target_user_id, channel_id)
        if target_role == "superadmin":
            await callback.answer("⚠️ Нельзя забанить владельца канала.", show_alert=True)
            return

    await state.update_data(
        ban_target_user_id=target_user_id,
        active_channel_id=channel_id
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1 день", callback_data="ban_dur:1"),
            InlineKeyboardButton(text="7 дней", callback_data="ban_dur:7")
        ],
        [
            InlineKeyboardButton(text="14 дней", callback_data="ban_dur:14"),
            InlineKeyboardButton(text="Навсегда", callback_data="ban_dur:36500")
        ],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="adm_back")]
    ])

    sent = await callback.message.answer(
        "📅 Длительность бана:",
        reply_markup=keyboard
    )
    await state.update_data(prompt_msg_id=sent.message_id)
    await state.set_state(AdminStates.waiting_ban_duration)
    await callback.answer()


# ══════════════════════════════════════════════════════════
# Эскалация репортов модератором
# ══════════════════════════════════════════════════════════

async def _handle_escalation(message: Message, state: FSMContext):
    data = await state.get_data()
    escalate_owner_id = data.get("escalate_owner_id")
    post_id = data.get("escalate_post_id")
    comment = message.text.strip() if message.text else ""

    if not comment:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Введите комментарий.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, "Пост уже удалён.")
            await state.set_state(None)
            return

        channel_title = await get_channel_title(session, post.channel_id)
        tz_name = await get_channel_timezone(session, post.channel_id)

    local_dt = utc_to_local(post.created_at, tz_name)
    dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

    report_text = (
        f"🛡 <b>Репорт от модератора</b>\n\n"
        f"📢 Канал: <b>{channel_title}</b>\n"
        f"📄 Публикация от: <code>{dt_str}</code>\n"
        f"💬 Комментарий: <i>{comment}</i>"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_del:{post.id}"),
            InlineKeyboardButton(
                text="🚫 Забанить",
                callback_data=f"adm_ban:{post.user_id}:{post.channel_id}"
            )
        ]
    ])

    try:
        await message.bot.send_message(
            chat_id=escalate_owner_id,
            text=report_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await delete_pair(message, data.get("prompt_msg_id"))
        await state.set_state(None)
        await state.update_data(
            escalate_post_id=None, escalate_owner_id=None, prompt_msg_id=None
        )
        await notify(message, "✅ Репорт отправлен владельцу.")
    except Exception:
        await delete_pair(message, data.get("prompt_msg_id"))
        await state.set_state(None)
        await state.update_data(
            escalate_post_id=None, escalate_owner_id=None, prompt_msg_id=None
        )
        await notify(message, "⚠️ Не удалось доставить репорт.")

    channel_id = data.get("active_channel_id")
    await enter_admin_panel(
        message.bot, message.chat.id,
        message.from_user.id, channel_id, state
    )


# ══════════════════════════════════════════════════════════
# Центр уведомлений и рассылок (Mailing System)
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data == "adm_notify_menu")
async def process_adm_notify_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Доступ ограничен.", show_alert=True)
            return

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])

    buttons = [
        [InlineKeyboardButton(text="👥 Рассылка всем пользователям", callback_data="adm_notify_all")],
        [InlineKeyboardButton(text="👤 Личное уведомление", callback_data="adm_notify_one")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]
    ]

    sent = await callback.message.answer(
        "📢 <b>Центр уведомлений и рассылок</b>\n\n"
        "Выберите тип рассылки:\n"
        "• <i>Рассылка всем</i> — отправка всем пользователям бота.\n"
        "• <i>Личное уведомление</i> — отправка конкретному участнику канала.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(admin_sub_msg_id=sent.message_id)


@router.callback_query(F.data == "adm_notify_all")
async def process_adm_notify_all(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Доступ ограничен.", show_alert=True)
            return

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])
        await state.update_data(admin_sub_msg_id=None)

    await state.set_state(AdminStates.waiting_notify_all_text)
    prompt = await callback.message.answer(
        "👥 <b>Рассылка всем пользователям</b>\n\n"
        "Введите текст сообщения для рассылки:\n"
        "<i>(Вы можете использовать HTML-теги для форматирования)</i>",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_notify_all_text)
async def process_notify_all_send(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    text_to_send = message.text.strip() if message.text else ""

    if not text_to_send:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Сообщение не может быть пустым.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User.user_id))
        user_ids = [row[0] for row in result.all()]

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await state.update_data(prompt_msg_id=None)

    # Запускаем рассылку в фоне, чтобы не вешать бота
    async def run_broadcast(bot_inst, target_ids, text_body):
        success_count = 0
        for uid in target_ids:
            try:
                await bot_inst.send_message(
                    chat_id=uid,
                    text=f"📢 <b>Объявление от администрации:</b>\n\n{text_body}",
                    parse_mode="HTML"
                )
                success_count += 1
                await asyncio.sleep(0.05)  # Защита от лимитов Telegram
            except Exception:
                pass
        try:
            await bot_inst.send_message(
                chat_id=message.from_user.id,
                text=f"✅ <b>Рассылка завершена!</b>\n\n"
                     f"Успешно отправлено пользователям: <code>{success_count}/{len(target_ids)}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass

    asyncio.create_task(run_broadcast(message.bot, user_ids, text_to_send))

    await notify(message, "🚀 Рассылка запущена в фоновом режиме.")
    await enter_admin_panel(message.bot, message.chat.id, message.from_user.id, channel_id, state)


@router.callback_query(F.data == "adm_notify_one")
async def process_adm_notify_one(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await check_admin_rights(session, callback.from_user.id, channel_id)
        if role != "superadmin":
            await callback.answer("Доступ ограничен.", show_alert=True)
            return

    if data.get("admin_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["admin_sub_msg_id"])
        await state.update_data(admin_sub_msg_id=None)

    await state.set_state(AdminStates.waiting_notify_one_target)
    prompt = await callback.message.answer(
        "👤 <b>Личное уведомление</b>\n\n"
        "Введите 9-значный внутренний ID пользователя в этом канале:",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)
    await callback.answer()


@router.message(AdminStates.waiting_notify_one_target)
async def process_notify_one_target(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    target_input = message.text.strip() if message.text else ""

    if not target_input or len(target_input) != 9:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Введите корректный 9-значный внутренний ID.")
        return

    target_user = None
    async with AsyncSessionLocal() as session:
        # Поиск только по internal_id
        cu_result = await session.execute(
            select(ChannelUser.user_id)
            .where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.internal_id == target_input
            )
        )
        user_tg_id = cu_result.scalar_one_or_none()
        if user_tg_id:
            result = await session.execute(select(User).where(User.user_id == user_tg_id))
            target_user = result.scalar_one_or_none()

    if not target_user:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "❌ Пользователь с таким внутренним ID не найден в этом канале.")
        return

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.update_data(notify_target_user_id=target_user.user_id)
    await state.set_state(AdminStates.waiting_notify_one_text)

    name_str = f"@{target_user.username}" if target_user.username else target_user.full_name
    prompt = await message.answer(
        f"👤 Получатель: <b>{name_str}</b> (ID: <code>{target_input}</code>)\n\n"
        "Введите текст сообщения для этого пользователя:\n"
        "<i>(Вы можете использовать HTML-теги)</i>",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


@router.message(AdminStates.waiting_notify_one_text)
async def process_notify_one_send(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    target_user_id = data.get("notify_target_user_id")
    text_to_send = message.text.strip() if message.text else ""

    if not text_to_send:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Сообщение не может быть пустым.")
        return

    delivered = True
    try:
        await message.bot.send_message(
            chat_id=target_user_id,
            text=f"✉️ <b>Личное уведомление от администрации:</b>\n\n{text_to_send}",
            parse_mode="HTML"
        )
    except Exception:
        delivered = False

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await state.update_data(prompt_msg_id=None, notify_target_user_id=None)

    if delivered:
        await notify(message, "✅ Уведомление доставлено!")
    else:
        await notify(message, "❌ Не удалось доставить сообщение (бот заблокирован или ID недействителен).", delay=5)

    await enter_admin_panel(message.bot, message.chat.id, message.from_user.id, channel_id, state)