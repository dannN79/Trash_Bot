import re
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    PreCheckoutQuery, LabeledPrice
)
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func, delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.database import AsyncSessionLocal
from db.models import User, Channel, ChannelUser, Post, Notification
from keyboards.reply import main_menu, back_only_keyboard
from utils import (
    safe_delete, cleanup_chat, notify, get_user_role, get_channel_title,
    get_channel_timezone, utc_to_local
)
from states.states import ReportPost, ChangeID

router = Router()


# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

async def get_or_create_user(session, tg_user) -> User:
    # Атомарный upsert — не боится гонки при одновременных запросах
    stmt = pg_insert(User).values(
        user_id=tg_user.id,
        username=tg_user.username,
        full_name=tg_user.full_name,
    ).on_conflict_do_update(
        index_elements=["user_id"],
        set_={
            "username": tg_user.username,
            "full_name": tg_user.full_name,
        }
    )
    await session.execute(stmt)
    result = await session.execute(select(User).where(User.user_id == tg_user.id))
    return result.scalar_one()


async def generate_channel_internal_id(session, channel_id: int) -> str:
    result = await session.execute(
        select(func.count()).where(ChannelUser.channel_id == channel_id)
    )
    count = result.scalar() or 0
    return f"{count + 1:09d}"


async def build_main_menu_text(session, user_id: int, active_channel_id: int | None) -> tuple[str, str]:
    if not active_channel_id:
        return (
            "👋 Добро пожаловать в <b>TrashTool Bot</b>!\n\n"
            "У вас пока нет подключённых каналов.\n"
            "Нажмите <b>🔄 Сменить канал</b> чтобы добавить первый канал.",
            "user"
        )

    role = await get_user_role(session, user_id, active_channel_id)
    ch_title = await get_channel_title(session, active_channel_id)

    cu_result = await session.execute(
        select(ChannelUser.internal_id).where(
            ChannelUser.user_id == user_id,
            ChannelUser.channel_id == active_channel_id
        )
    )
    internal_id = cu_result.scalar_one_or_none() or "—"

    role_labels = {
        "superadmin": "👑 Супер-админ",
        "moderator":  "🛡 Модератор",
        "user":       "👤 Пользователь",
    }

    text = (
        f"🏠 <b>Главное меню</b>\n\n"
        f"📢 Канал: <b>{ch_title}</b>\n"
        f"🆔 Ваш ID в канале: <code>{internal_id}</code>\n"
        f"🎭 Роль: {role_labels.get(role, role)}"
    )
    return text, role


async def show_main_menu_msg(bot, chat_id: int, user_id: int, state: FSMContext):
    """Очищает чат и показывает главное меню."""
    await cleanup_chat(bot, chat_id, state)

    data = await state.get_data()
    active_channel_id = data.get("active_channel_id")

    # Если active_channel_id потерялся (перезапуск бота / MemoryStorage) — восстанавливаем из БД
    if not active_channel_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ChannelUser.channel_id).where(
                    ChannelUser.user_id == user_id
                ).order_by(ChannelUser.channel_id)
            )
            rows = result.fetchall()
        if rows:
            active_channel_id = rows[0][0]
            await state.update_data(active_channel_id=active_channel_id)

    async with AsyncSessionLocal() as session:
        text, role = await build_main_menu_text(session, user_id, active_channel_id)

    sent = await bot.send_message(chat_id, text, reply_markup=main_menu(role), parse_mode="HTML")
    await state.update_data(main_menu_msg_id=sent.message_id)


# ──────────────────────────────────────────────
# /start
# ──────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(None)

    async with AsyncSessionLocal() as session:
        await get_or_create_user(session, message.from_user)
        await session.commit()

        result = await session.execute(
            select(ChannelUser.channel_id).where(
                ChannelUser.user_id == message.from_user.id
            )
        )
        channel_ids = [row[0] for row in result.all()]

    data = await state.get_data()
    active_channel_id = data.get("active_channel_id")
    if active_channel_id not in channel_ids:
        active_channel_id = channel_ids[0] if channel_ids else None
        await state.update_data(active_channel_id=active_channel_id)

    # Удаляем сообщение /start пользователя
    await safe_delete(message.bot, message.chat.id, message.message_id)
    await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)
    await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)


# ──────────────────────────────────────────────
# Пересылка сообщения из канала → регистрация
# ──────────────────────────────────────────────

@router.message(F.forward_from_chat, StateFilter(None))
async def handle_forwarded_channel(message: Message, state: FSMContext):
    chat = message.forward_from_chat

    if chat.type != "channel":
        await notify(message, "⚠️ Перешлите сообщение именно из канала.", delay=4)
        await safe_delete(message.bot, message.chat.id, message.message_id)
        return

    async with AsyncSessionLocal() as session:
        channel_result = await session.execute(
            select(Channel).where(Channel.channel_id == chat.id)
        )
        channel = channel_result.scalar_one_or_none()

        # ── Канал НЕ зарегистрирован ──
        if not channel:
            try:
                member = await message.bot.get_chat_member(chat.id, message.from_user.id)
                is_admin = member.status in ("administrator", "creator")
            except Exception:
                is_admin = False

            if not is_admin:
                await notify(
                    message,
                    f"❌ Канал <b>{chat.title}</b> не поддерживает TrashTool Bot.\n\n"
                    "Если вы владелец — добавьте бота как администратора "
                    "и перешлите сообщение снова.",
                    delay=6
                )
                await safe_delete(message.bot, message.chat.id, message.message_id)
                return

            await get_or_create_user(session, message.from_user)

            new_channel = Channel(
                channel_id=chat.id,
                title=chat.title,
                owner_id=message.from_user.id,
            )
            session.add(new_channel)
            await session.flush()

            internal_id = await generate_channel_internal_id(session, chat.id)
            cu = ChannelUser(
                channel_id=chat.id,
                user_id=message.from_user.id,
                role="superadmin",
                internal_id=internal_id,
            )
            session.add(cu)
            await session.commit()

            await state.update_data(active_channel_id=chat.id)
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(
                message,
                f"✅ Канал <b>{chat.title}</b> зарегистрирован!\n"
                f"🆔 Ваш ID: <code>{internal_id}</code> | 👑 Супер-админ",
                delay=5
            )
            await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)
            return

        # ── Канал зарегистрирован ──
        await get_or_create_user(session, message.from_user)

        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.channel_id == chat.id,
                ChannelUser.user_id == message.from_user.id,
            )
        )
        existing_cu = cu_result.scalar_one_or_none()

        if existing_cu:
            if message.forward_from_message_id:
                # Попробуем найти этот пост в БД
                post_res = await session.execute(
                    select(Post).where(
                        Post.channel_id == chat.id,
                        Post.message_id == message.forward_from_message_id,
                        Post.is_deleted == False
                    )
                )
                post = post_res.scalar_one_or_none()
                if post:
                    await state.update_data(
                        active_channel_id=chat.id,
                        report_target_post_id=post.id
                    )
                    await state.set_state(ReportPost.waiting_reason)
                    await safe_delete(message.bot, message.chat.id, message.message_id)

                    tz_name = await get_channel_timezone(session, chat.id)
                    local_dt = utc_to_local(post.created_at, tz_name)
                    dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

                    prompt = await message.answer(
                        f"⚠️ <b>Подача жалобы на публикацию</b>\n\n"
                        f"Выбрана публикация от {dt_str}.\n"
                        f"Введите причину жалобы:",
                        reply_markup=back_only_keyboard(),
                        parse_mode="HTML"
                    )
                    await state.update_data(prompt_msg_id=prompt.message_id)
                    return

            await state.update_data(active_channel_id=chat.id)
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(
                message,
                f"ℹ️ Вы уже в канале <b>{chat.title}</b>. Канал установлен как активный.",
                delay=4
            )
        else:
            internal_id = await generate_channel_internal_id(session, chat.id)
            role = "superadmin" if channel.owner_id == message.from_user.id else "user"
            cu = ChannelUser(
                channel_id=chat.id,
                user_id=message.from_user.id,
                role=role,
                internal_id=internal_id,
            )
            session.add(cu)
            await session.commit()

            await state.update_data(active_channel_id=chat.id)
            await safe_delete(message.bot, message.chat.id, message.message_id)
            role_label = "👑 Супер-админ" if role == "superadmin" else "👤 Пользователь"
            await notify(
                message,
                f"✅ Вы присоединились к <b>{chat.title}</b>!\n"
                f"🆔 ID: <code>{internal_id}</code> | {role_label}",
                delay=5
            )

        await session.commit()

    await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)


# ──────────────────────────────────────────────
# 🔄 Сменить канал
# ──────────────────────────────────────────────

@router.message(Command("channels"))
@router.message(F.text == "🔄 Сменить канал")
async def cmd_channels(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)
    await cleanup_chat(message.bot, message.chat.id, state)

    data = await state.get_data()
    active_channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Channel.channel_id, Channel.title, ChannelUser.role, ChannelUser.internal_id)
            .join(ChannelUser, ChannelUser.channel_id == Channel.channel_id)
            .where(ChannelUser.user_id == message.from_user.id)
        )
        channels = result.all()

    role_icons = {"superadmin": "👑", "moderator": "🛡", "user": "👤"}
    buttons = []

    if channels:
        for ch_id, title, role, internal_id in channels:
            prefix = "✅ " if ch_id == active_channel_id else "   "
            icon = role_icons.get(role, "👤")
            buttons.append([InlineKeyboardButton(
                text=f"{prefix}{title}  {icon}",
                callback_data=f"switch_ch:{ch_id}"
            )])
    else:
        buttons.append([InlineKeyboardButton(
            text="📭 У вас нет каналов",
            callback_data="noop"
        )])

    buttons.append([InlineKeyboardButton(
        text="➕ Добавить канал",
        callback_data="add_channel_info"
    )])

    if channels:
        buttons.append([InlineKeyboardButton(
            text="🗑 Отключиться от канала",
            callback_data="leave_channel_menu"
        )])

    buttons.append([InlineKeyboardButton(
        text="🔙 Назад",
        callback_data="ch_back_main"
    )])

    sent = await message.answer(
        "🔄 <b>Управление каналами</b>\n\n"
        "Нажмите на канал, чтобы переключиться:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(channels_msg_id=sent.message_id)


@router.callback_query(F.data == "noop")
async def process_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("switch_ch:"))
async def process_switch_channel(callback: CallbackQuery, state: FSMContext):
    channel_id = int(callback.data.split(":")[1])

    data = await state.get_data()
    if data.get("active_channel_id") == channel_id:
        await callback.answer("Этот канал уже активен.")
        return

    async with AsyncSessionLocal() as session:
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == callback.from_user.id,
                ChannelUser.channel_id == channel_id,
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu:
            await callback.answer("Канал не найден.", show_alert=True)
            return

    await state.update_data(active_channel_id=channel_id)
    await callback.answer("✅ Канал переключён!")
    await show_main_menu_msg(callback.bot, callback.message.chat.id, callback.from_user.id, state)


@router.callback_query(F.data == "add_channel_info")
async def process_add_channel_info(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if data.get("channels_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])

    sent = await callback.message.answer(
        "➕ <b>Как подключить канал:</b>\n\n"
        "🔹 <b>Если канал уже подключён к боту администрацией:</b>\n"
        "1️⃣ Просто <b>перешлите сюда любое сообщение</b> из этого канала, чтобы присоединиться как участник.\n\n"
        "👑 <b>Если вы владелец нового канала и хотите подключить его впервые:</b>\n"
        "1️⃣ Добавьте бота <b>в качестве администратора</b> в ваш канал (с правами на публикацию/удаление сообщений).\n"
        "2️⃣ <b>Перешлите сюда любое сообщение</b> из этого канала для регистрации.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к каналам", callback_data="back_to_channels")]
        ]),
        parse_mode="HTML"
    )
    await state.update_data(channels_msg_id=sent.message_id)


@router.callback_query(F.data == "back_to_channels")
async def process_back_to_channels(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if data.get("channels_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])
        await state.update_data(channels_msg_id=None)
    # Перестроим меню каналов
    active_channel_id = data.get("active_channel_id")
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Channel.channel_id, Channel.title, ChannelUser.role, ChannelUser.internal_id)
            .join(ChannelUser, ChannelUser.channel_id == Channel.channel_id)
            .where(ChannelUser.user_id == callback.from_user.id)
        )
        channels = result.all()

    role_icons = {"superadmin": "👑", "moderator": "🛡", "user": "👤"}
    buttons = []
    if channels:
        for ch_id, title, role, internal_id in channels:
            prefix = "✅ " if ch_id == active_channel_id else "   "
            icon = role_icons.get(role, "👤")
            buttons.append([InlineKeyboardButton(
                text=f"{prefix}{title}  {icon}",
                callback_data=f"switch_ch:{ch_id}"
            )])
    else:
        buttons.append([InlineKeyboardButton(text="📭 У вас нет каналов", callback_data="noop")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel_info")])
    if channels:
        buttons.append([InlineKeyboardButton(text="🗑 Отключиться от канала", callback_data="leave_channel_menu")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="ch_back_main")])

    sent = await callback.message.answer(
        "🔄 <b>Управление каналами</b>\n\n"
        "Нажмите на канал, чтобы переключиться:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(channels_msg_id=sent.message_id)


@router.callback_query(F.data == "leave_channel_menu")
async def process_leave_channel_menu(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    if data.get("channels_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Channel.channel_id, Channel.title, ChannelUser.role)
            .join(ChannelUser, ChannelUser.channel_id == Channel.channel_id)
            .where(ChannelUser.user_id == callback.from_user.id)
        )
        channels = result.all()

    buttons = []
    for ch_id, title, role in channels:
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {title}",
            callback_data=f"leave_ch:{ch_id}"
        )])
    buttons.append([InlineKeyboardButton(text="🔙 Назад к каналам", callback_data="back_to_channels")])

    sent = await callback.message.answer(
        "🗑 <b>Отключиться от канала</b>\n\n"
        "Выберите канал, от которого хотите отключиться:\n"
        "<i>⚠️ Ваши посты в канале останутся, но вы потеряете доступ к управлению.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(channels_msg_id=sent.message_id)


@router.callback_query(F.data.startswith("leave_ch:"))
async def process_leave_channel(callback: CallbackQuery, state: FSMContext):
    channel_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    async with AsyncSessionLocal() as session:
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == callback.from_user.id,
                ChannelUser.channel_id == channel_id,
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu:
            await callback.answer("Вы не подключены к этому каналу.", show_alert=True)
            return

        if cu.role == "superadmin":
            # Спрашиваем подтверждение удаления канала или выхода из него
            buttons = [
                [InlineKeyboardButton(text="🗑 Удалить только у себя (выйти)", callback_data=f"leave_ch_self:{channel_id}")],
                [InlineKeyboardButton(text="💥 Удалить канал из бота полностью (для всех)", callback_data=f"confirm_del_ch:{channel_id}")],
                [InlineKeyboardButton(text="🔙 Отмена", callback_data="back_to_channels")]
            ]
            ch_title = await get_channel_title(session, channel_id)
            
            if data.get("channels_msg_id"):
                await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])
                
            sent = await callback.message.answer(
                f"⚠️ <b>Внимание!</b>\n\n"
                f"Вы являетесь владельцем канала <b>{ch_title}</b>.\n"
                f"Выберите действие:\n"
                f"• <b>Удалить только у себя:</b> вы выйдете из списка участников канала в боте, но сам канал, посты, баны и другие модераторы останутся.\n"
                f"• <b>Удалить для всех:</b> канал полностью удалится из базы данных бота со всей информацией.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
                parse_mode="HTML"
            )
            await state.update_data(channels_msg_id=sent.message_id)
            await callback.answer()
            return

        await session.delete(cu)
        await session.commit()

    # Если это был активный канал — переключаемся
    if data.get("active_channel_id") == channel_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ChannelUser.channel_id).where(
                    ChannelUser.user_id == callback.from_user.id
                )
            )
            remaining = [row[0] for row in result.all()]
        new_active = remaining[0] if remaining else None
        await state.update_data(active_channel_id=new_active)

    await callback.answer("✅ Вы отключились от канала.")
    await show_main_menu_msg(callback.bot, callback.message.chat.id, callback.from_user.id, state)


@router.callback_query(F.data.startswith("leave_ch_self:"))
async def process_leave_channel_self(callback: CallbackQuery, state: FSMContext):
    channel_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    async with AsyncSessionLocal() as session:
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == callback.from_user.id,
                ChannelUser.channel_id == channel_id,
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu:
            await callback.answer("Вы не подключены к этому каналу.", show_alert=True)
            return

        await session.delete(cu)
        await session.commit()

    # Если это был активный канал — переключаемся
    if data.get("active_channel_id") == channel_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ChannelUser.channel_id).where(
                    ChannelUser.user_id == callback.from_user.id
                )
            )
            remaining = [row[0] for row in result.all()]
        new_active = remaining[0] if remaining else None
        await state.update_data(active_channel_id=new_active)

    if data.get("channels_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])
        await state.update_data(channels_msg_id=None)

    await callback.answer("✅ Вы отключились от канала.")
    await show_main_menu_msg(callback.bot, callback.message.chat.id, callback.from_user.id, state)


@router.callback_query(F.data.startswith("confirm_del_ch:"))
async def process_confirm_delete_channel(callback: CallbackQuery, state: FSMContext):
    channel_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    async with AsyncSessionLocal() as session:
        # Проверяем роль еще раз на всякий случай
        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == callback.from_user.id,
                ChannelUser.channel_id == channel_id,
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu or cu.role != "superadmin":
            await callback.answer("Ошибка: нет прав или канал не найден.", show_alert=True)
            return

        # Удаляем связанные уведомления
        await session.execute(
            sa_delete(Notification).where(Notification.channel_id == channel_id)
        )

        # Удаляем канал из таблицы channels ( CASCADE удалит связанные записи )
        await session.execute(
            sa_delete(Channel).where(Channel.channel_id == channel_id)
        )
        await session.commit()

    # Сбрасываем/меняем активный канал
    if data.get("active_channel_id") == channel_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ChannelUser.channel_id).where(
                    ChannelUser.user_id == callback.from_user.id
                )
            )
            remaining = [row[0] for row in result.all()]
        new_active = remaining[0] if remaining else None
        await state.update_data(active_channel_id=new_active)

    if data.get("channels_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])
        await state.update_data(channels_msg_id=None)

    await callback.answer("✅ Канал полностью удалён из бота.", show_alert=True)
    await show_main_menu_msg(callback.bot, callback.message.chat.id, callback.from_user.id, state)


@router.callback_query(F.data == "ch_back_main")
async def process_ch_back_main(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_main_menu_msg(callback.bot, callback.message.chat.id, callback.from_user.id, state)


# ──────────────────────────────────────────────
# Вход в Админ-панель
# ──────────────────────────────────────────────

@router.message(F.text == "🔧 Админ-панель")
async def admin_panel_trigger(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)

    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    # Если active_channel_id потерялся после перезапуска — ищем в БД первый канал где юзер — админ
    if not channel_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ChannelUser.channel_id).where(
                    ChannelUser.user_id == message.from_user.id,
                    ChannelUser.role.in_(("moderator", "superadmin"))
                ).order_by(ChannelUser.channel_id)
            )
            row = result.first()
        if row:
            channel_id = row[0]
            await state.update_data(active_channel_id=channel_id)

    if not channel_id:
        await notify(message, "⚠️ Сначала выберите активный канал.")
        return

    async with AsyncSessionLocal() as session:
        role = await get_user_role(session, message.from_user.id, channel_id)

    if role not in ("moderator", "superadmin"):
        await notify(message, "❌ Доступ запрещен.")
        return

    from handlers.admin import enter_admin_panel
    await enter_admin_panel(message.bot, message.chat.id, message.from_user.id, channel_id, state)


# ──────────────────────────────────────────────
# Главное меню: Кнопки Правила и Сменить ID
# ──────────────────────────────────────────────

@router.message(F.text == "📜 Правила")
async def cmd_rules(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)
    await cleanup_chat(message.bot, message.chat.id, state)
    
    text = (
        "📜 <b>Правила использования бота</b>\n\n"
        "1️⃣ <b>Запрещено размещение контента 18+</b> (порнография, эротика и т.д.).\n"
        "2️⃣ <b>Запрещена несанкционированная реклама</b> в любых проявлениях.\n"
        "3️⃣ <b>Запрещен спам</b>, флуд и массовые бессмысленные публикации.\n\n"
        "⚠️ <b>Отказ от ответственности:</b>\n"
        "Администрация и создатели бота не несут ответственности за действия пользователей, публикуемый контент или возможный ущерб, связанный с использованием данного сервиса. Вся ответственность за содержание публикаций лежит исключительно на их авторах.\n\n"
        "<i>Нарушение правил повлечет за собой перманентную блокировку в боте.</i>"
    )
    
    sent = await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="ch_back_main")]
        ]),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=sent.message_id)


@router.message(F.text == "🆔 Сменить ID (250 ⭐️)")
async def cmd_change_id(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)
    await cleanup_chat(message.bot, message.chat.id, state)

    data = await state.get_data()
    active_channel_id = data.get("active_channel_id")
    if not active_channel_id:
        await notify(message, "⚠️ Сначала выберите активный канал в разделе '🔄 Сменить канал'.", delay=4)
        return

    prompt = await message.answer(
        "🆔 <b>Смена уникального ID в канале</b>\n\n"
        "Стоимость смены ID составляет <b>250 ⭐️ (Telegram Stars)</b>.\n"
        "Введите новый желаемый ID (от 3 до 20 символов, только латинские буквы, цифры и подчеркивания):\n\n"
        "<i>Бот предварительно проверит его доступность перед выставлением счета.</i>",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(ChangeID.waiting_new_id)
    await state.update_data(prompt_msg_id=prompt.message_id)


# ──────────────────────────────────────────────
# Смена ID за Telegram Stars
# ──────────────────────────────────────────────

@router.callback_query(F.data == "buy_change_id")
async def process_buy_change_id(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    active_channel_id = data.get("active_channel_id")
    if not active_channel_id:
        await callback.answer("⚠️ Сначала выберите активный канал.", show_alert=True)
        return

    await callback.answer()
    
    # Стираем старое сообщение меню каналов
    if data.get("channels_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["channels_msg_id"])
        await state.update_data(channels_msg_id=None)

    prompt = await callback.message.answer(
        "🆔 <b>Смена уникального ID в канале</b>\n\n"
        "Стоимость смены ID составляет <b>250 ⭐️ (Telegram Stars)</b>.\n"
        "Введите новый желаемый ID (от 3 до 20 символов, только латинские буквы, цифры и подчеркивания):\n\n"
        "<i>Бот предварительно проверит его доступность перед выставлением счета.</i>",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.set_state(ChangeID.waiting_new_id)
    await state.update_data(prompt_msg_id=prompt.message_id)


@router.message(ChangeID.waiting_new_id)
async def process_input_new_id(message: Message, state: FSMContext):
    data = await state.get_data()
    active_channel_id = data.get("active_channel_id")
    
    # Удаляем сообщение пользователя и подсказку
    await safe_delete(message.bot, message.chat.id, message.message_id)
    if data.get("prompt_msg_id"):
        await safe_delete(message.bot, message.chat.id, data["prompt_msg_id"])
        await state.update_data(prompt_msg_id=None)

    if message.text == "🔙 Назад":
        await state.set_state(None)
        await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)
        return

    new_id = message.text.strip()
    
    # Валидация ID (латинские буквы, цифры, подчеркивания)
    if not re.match(r"^[a-zA-Z0-9_]{3,20}$", new_id):
        prompt = await message.answer(
            "❌ <b>Неверный формат ID!</b>\n\n"
            "ID должен состоять из латинских букв, цифр и символа подчеркивания, длиной от 3 до 20 символов.\n"
            "Попробуйте еще раз:",
            reply_markup=back_only_keyboard(),
            parse_mode="HTML"
        )
        await state.update_data(prompt_msg_id=prompt.message_id)
        return

    async with AsyncSessionLocal() as session:
        # Проверяем занят ли ID в текущем канале
        result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.channel_id == active_channel_id,
                ChannelUser.internal_id == new_id
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            # Если это тот же самый пользователь, который уже владеет этим ID
            if existing.user_id == message.from_user.id:
                await notify(message, "ℹ️ Вы уже используете этот ID в данном канале.", delay=4)
                await state.set_state(None)
                await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)
                return
                
            prompt = await message.answer(
                f"❌ ID <code>{new_id}</code> в данном канале уже занят другим пользователем!\n"
                f"Пожалуйста, введите другой ID:",
                reply_markup=back_only_keyboard(),
                parse_mode="HTML"
            )
            await state.update_data(prompt_msg_id=prompt.message_id)
            return

    # Если ID свободен, отправляем счет (Invoice) на оплату Telegram Stars!
    await state.set_state(None)
    
    # Выставляем счет
    prices = [LabeledPrice(label="Смена ID", amount=250)]
    
    try:
        sent_invoice = await message.bot.send_invoice(
            chat_id=message.chat.id,
            title="🆔 Смена ID в канале",
            description=f"Смена вашего уникального ID в канале на: {new_id}",
            payload=f"change_internal_id:{active_channel_id}:{new_id}",
            provider_token="",
            currency="XTR",
            prices=prices,
            need_name=False,
            need_phone_number=False,
            need_email=False,
            need_shipping_address=False,
            is_flexible=False
        )
        await state.update_data(invoice_msg_id=sent_invoice.message_id)
    except Exception as e:
        await message.answer(f"❌ Ошибка при выставлении счета: {e}")
        await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)


@router.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload
    if payload.startswith("change_internal_id:"):
        _, channel_id_str, new_id = payload.split(":")
        channel_id = int(channel_id_str)
        
        # Еще раз проверяем, свободен ли ID
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ChannelUser).where(
                    ChannelUser.channel_id == channel_id,
                    ChannelUser.internal_id == new_id
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                await pre_checkout_query.bot.answer_pre_checkout_query(
                    pre_checkout_query.id,
                    ok=False,
                    error_message="К сожалению, этот ID уже занят кем-то другим."
                )
                return
                
        await pre_checkout_query.bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message, state: FSMContext):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("change_internal_id:"):
        _, channel_id_str, new_id = payload.split(":")
        channel_id = int(channel_id_str)
        user_id = message.from_user.id
        
        async with AsyncSessionLocal() as session:
            # Находим запись пользователя в канале
            cu_result = await session.execute(
                select(ChannelUser).where(
                    ChannelUser.channel_id == channel_id,
                    ChannelUser.user_id == user_id
                )
            )
            cu = cu_result.scalar_one_or_none()
            if cu:
                old_id = cu.internal_id
                cu.internal_id = new_id
                await session.commit()
                
                await message.answer(
                    f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                    f"Ваш ID в канале успешно изменен с <code>{old_id}</code> на <code>{new_id}</code>.",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    f"❌ Произошла ошибка: запись о вашем участии в канале не найдена."
                )
                
        # Очищаем инвойс и показываем главное меню
        data = await state.get_data()
        if data.get("invoice_msg_id"):
            await safe_delete(message.bot, message.chat.id, data["invoice_msg_id"])
            await state.update_data(invoice_msg_id=None)
            
        await show_main_menu_msg(message.bot, message.chat.id, message.from_user.id, state)