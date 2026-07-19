from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy import select, func, delete as sa_delete, update

from db.database import AsyncSessionLocal
from db.models import User, Channel, ChannelUser, Notification, Post
from states.states import NotificationStates
from keyboards.reply import main_menu, back_only_keyboard
from utils import (
    safe_delete, cleanup_chat, notify, delete_pair, edit_or_resend,
    get_channel_timezone, get_channel_title, get_user_role, utc_to_local, utcnow,
)

router = Router()

# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

async def show_notifications_panel(bot, chat_id: int, user_id: int, state: FSMContext):
    """Показывает панель управления уведомлениями."""
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    if not channel_id:
        return

    # Очищаем чат от предыдущих сообщений уведомлений
    for key in ("notification_panel_msg_id", "notification_sub_msg_id"):
        if data.get(key):
            await safe_delete(bot, chat_id, data[key])
    await state.update_data(notification_panel_msg_id=None, notification_sub_msg_id=None)

    async with AsyncSessionLocal() as session:
        role = await get_user_role(session, user_id, channel_id)
        
        # Считаем непрочитанные уведомления только для активного канала
        unread_count = await session.scalar(
            select(func.count(Notification.id)).where(
                Notification.receiver_id == user_id,
                Notification.channel_id == channel_id,
                Notification.is_read == False
            )
        ) or 0

    text = (
        f"🔔 <b>Центр уведомлений</b>\n\n"
        f"У вас <b>{unread_count}</b> непрочитанных уведомлений.\n\n"
        f"Вы можете просматривать входящие сообщения и отправлять новые уведомления другим участникам бота."
    )

    buttons = [
        [InlineKeyboardButton(text=f"📥 Входящие ({unread_count})", callback_data="ntf_inbox")],
        [InlineKeyboardButton(text="📤 Отправить уведомление", callback_data="ntf_send_choose")],
        [InlineKeyboardButton(text="✖️ Закрыть", callback_data="ntf_close")]
    ]

    sent = await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(notification_panel_msg_id=sent.message_id)


# ──────────────────────────────────────────────
# Хэндлер кнопки «🔔 Уведомления»
# ──────────────────────────────────────────────

@router.message(F.text == "🔔 Уведомления")
async def cmd_notifications_menu(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)

    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    if not channel_id:
        await notify(message, "⚠️ Сначала подключите канал.", delay=4)
        return

    await show_notifications_panel(message.bot, message.chat.id, message.from_user.id, state)


# ──────────────────────────────────────────────
# Callback: Входящие сообщения (Inbox)
# ──────────────────────────────────────────────

@router.callback_query(F.data == "ntf_inbox")
async def process_ntf_inbox(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    # Стираем пересланные сообщения, если они были
    fwd = data.get("notification_fwd_msg_id")
    if fwd:
        if isinstance(fwd, list):
            for m_id in fwd:
                await safe_delete(callback.bot, callback.message.chat.id, m_id)
        else:
            await safe_delete(callback.bot, callback.message.chat.id, fwd)
        await state.update_data(notification_fwd_msg_id=None)

    async with AsyncSessionLocal() as session:
        # Достаем последние 10 уведомлений пользователя для текущего активного канала
        result = await session.execute(
            select(Notification)
            .where(
                Notification.receiver_id == callback.from_user.id,
                Notification.channel_id == channel_id
            )
            .order_by(Notification.is_read.asc(), Notification.created_at.desc())
            .limit(10)
        )
        notifications = result.scalars().all()
        tz_name = await get_channel_timezone(session, channel_id)

    buttons = []
    if notifications:
        for ntf in notifications:
            status = "✉️" if not ntf.is_read else "📖"
            local_dt = utc_to_local(ntf.created_at, tz_name)
            date_str = local_dt.strftime("%d.%m %H:%M") if local_dt else "—"
            
            # Обрезаем текст для кнопки
            preview = ntf.text[:20].replace("\n", " ")
            if len(ntf.text) > 20:
                preview += "..."
                
            buttons.append([InlineKeyboardButton(
                text=f"{status} {date_str} | {preview}",
                callback_data=f"ntf_view:{ntf.id}"
            )])
    else:
        buttons.append([InlineKeyboardButton(text="📭 Входящих нет", callback_data="noop")])

    buttons.append([InlineKeyboardButton(text="🔙 Назад к панели", callback_data="ntf_back_panel")])

    if data.get("notification_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["notification_sub_msg_id"])

    sent = await callback.message.answer(
        "📥 <b>Входящие уведомления (последние 10)</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(notification_sub_msg_id=sent.message_id)
    await callback.answer()


# ──────────────────────────────────────────────
# Callback: Просмотр конкретного уведомления
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("ntf_view:"))
async def process_ntf_view(callback: CallbackQuery, state: FSMContext):
    ntf_id = int(callback.data.split(":")[1])
    data = await state.get_data()

    # Сначала удалим предыдущий пересланный пост жалобы, если был
    old_fwd = data.get("notification_fwd_msg_id")
    if old_fwd:
        if isinstance(old_fwd, list):
            for m_id in old_fwd:
                await safe_delete(callback.bot, callback.message.chat.id, m_id)
        else:
            await safe_delete(callback.bot, callback.message.chat.id, old_fwd)
        await state.update_data(notification_fwd_msg_id=None)

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.id == ntf_id))
        ntf = result.scalar_one_or_none()
        
        if not ntf or ntf.receiver_id != callback.from_user.id:
            await callback.answer("Уведомление не найдено.", show_alert=True)
            return

        # Помечаем прочитанным
        if not ntf.is_read:
            ntf.is_read = True
            ntf.read_at = utcnow()
            await session.commit()

        ch_id = ntf.channel_id
        tz_name = await get_channel_timezone(session, ch_id)
        local_dt = utc_to_local(ntf.created_at, tz_name)
        date_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

        sender_info = "Система"
        if ntf.sender_id:
            # Находим internal_id отправителя в контекстном канале уведомления
            cu_result = await session.execute(
                select(ChannelUser.internal_id, ChannelUser.role).where(
                    ChannelUser.channel_id == ch_id,
                    ChannelUser.user_id == ntf.sender_id
                )
            )
            cu = cu_result.first()
            if cu:
                sender_info = f"ID: {cu.internal_id} ({cu.role})"
            else:
                sender_info = "Участник (ID скрыт)"

        # Проверяем роль получателя в контекстном канале
        my_role = await get_user_role(session, callback.from_user.id, ch_id)

        # Название канала
        ch_title = "Система"
        if ntf.channel_id:
            ch_title = await get_channel_title(session, ntf.channel_id)

        # Вычисляем тип уведомления
        ntf_type = "Уведомление"
        if ntf.post_id is not None:
            ntf_type = "⚠️ Жалоба на публикацию"
        else:
            t_lower = ntf.text.lower() if ntf.text else ""
            if "объявление от администрации" in t_lower:
                ntf_type = "📢 Объявление / Рассылка"
            elif "обращение к владельцу" in t_lower:
                ntf_type = "✉️ Обращение к владельцу"
            elif "обращение к модераторам" in t_lower:
                ntf_type = "✉️ Обращение к модераторам"
            elif "был удалён модератором" in t_lower or "была удалена модератором" in t_lower or "автобан" in t_lower:
                ntf_type = "🚫 Модерация (удаление/бан)"
            elif "личное уведомление" in t_lower:
                ntf_type = "✉️ Личное сообщение"

        # Информация о том, кто принял жалобу
        accepted_info = None
        if ntf.accepted_by:
            cu_acc = await session.execute(
                select(ChannelUser.internal_id, ChannelUser.role).where(
                    ChannelUser.channel_id == ch_id,
                    ChannelUser.user_id == ntf.accepted_by
                )
            )
            cu_a = cu_acc.first()
            if cu_a:
                accepted_info = f"ID: {cu_a.internal_id} ({cu_a.role})"
            else:
                accepted_info = "Участник (ID скрыт)"

        # Пересылка поста (если это жалоба и пользователь админ/модер)
        post_found = False
        forwarded_msg_id = None
        if ntf.post_id and my_role in ("moderator", "superadmin"):
            post_result = await session.execute(select(Post).where(Post.id == ntf.post_id))
            post = post_result.scalar_one_or_none()
            if post and not post.is_deleted:
                import json
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
                    forwarded_msg_id = fwd_ids if len(fwd_ids) > 1 else (fwd_ids[0] if fwd_ids else None)
                    post_found = True
                    await state.update_data(notification_fwd_msg_id=forwarded_msg_id)
                except Exception:
                    post_found = False

    text = (
        f"✉️ <b>Уведомление</b>\n"
        f"📢 Канал: <b>{ch_title}</b>\n"
        f"🏷 Тип: <b>{ntf_type}</b>\n"
        f"👤 Отправитель: <code>{sender_info}</code>\n"
        f"📅 Дата: <code>{date_str}</code>\n"
        f"───────────────────\n\n"
        f"{ntf.text}"
    )

    if ntf.post_id and my_role in ("moderator", "superadmin"):
        if post_found:
            text += "\n\n📌 <b>Пост переслан выше 👆</b>"
        else:
            text += "\n\n❌ <b>Пост, на который пожаловались, не найден в канале или был удален.</b>"

        if accepted_info:
            text += f"\n\n📥 <b>Жалоба принята в работу:</b> <code>{accepted_info}</code>"
        else:
            text += "\n\n📥 <b>Жалоба ожидает обработки.</b>"

    buttons = []
    
    # Если уведомление содержит прикрепленный пост (жалоба) и пользователь — админ/модер
    if ntf.post_id and my_role in ("moderator", "superadmin"):
        row1 = []
        if not ntf.accepted_by:
            row1.append(InlineKeyboardButton(text="📥 Взять в работу", callback_data=f"adm_take_ntf:{ntf.id}"))
        buttons.append(row1)
        
        buttons.append([
            InlineKeyboardButton(text="🗑 Удалить пост", callback_data=f"adm_del:{ntf.post_id}"),
            InlineKeyboardButton(text="🚫 Забанить автора", callback_data=f"adm_ban_from_ntf:{ntf.id}")
        ])

    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить из списка", callback_data=f"ntf_delete:{ntf.id}"),
        InlineKeyboardButton(text="🔙 Назад к списку", callback_data="ntf_inbox")
    ])

    if data.get("notification_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["notification_sub_msg_id"])

    sent = await callback.message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(notification_sub_msg_id=sent.message_id)
    await callback.answer()


# ──────────────────────────────────────────────
# Callback: Специальный бан из уведомления-жалобы
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_ban_from_ntf:"))
async def process_adm_ban_from_ntf(callback: CallbackQuery, state: FSMContext):
    ntf_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.id == ntf_id))
        ntf = result.scalar_one_or_none()
        if not ntf or not ntf.post_id:
            await callback.answer("Пост не найден.", show_alert=True)
            return

        post_result = await session.execute(select(Post).where(Post.id == ntf.post_id))
        post = post_result.scalar_one_or_none()
        if not post:
            await callback.answer("Публикация не найдена.", show_alert=True)
            return
        
        target_user_id = post.user_id
        # Используем channel_id из самого поста, а не из активного канала пользователя
        ban_channel_id = post.channel_id

        target_role = await get_user_role(session, target_user_id, ban_channel_id)
        if target_role == "superadmin":
            await callback.answer("⚠️ Нельзя забанить супер-админа (владельца канала).", show_alert=True)
            return

        # Проверяем, что текущий пользователь является админом/модером именно этого канала
        our_role = await get_user_role(session, callback.from_user.id, ban_channel_id)
        if our_role not in ("moderator", "superadmin"):
            await callback.answer("⛔️ Нет прав модерировать этот канал.", show_alert=True)
            return

        tz_name = await get_channel_timezone(session, ban_channel_id)
        local_dt = utc_to_local(post.created_at, tz_name)
        dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

    # Перенаправляем на флоу бана в admin.py — используем channel_id поста
    await state.update_data(
        ban_target_user_id=target_user_id,
        active_channel_id=ban_channel_id
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
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="ntf_inbox")]
    ])

    sent = await callback.message.answer(
        f"📅 Выберите длительность бана для автора публикации от {dt_str}:",
        reply_markup=keyboard
    )
    await state.update_data(prompt_msg_id=sent.message_id)
    from states.states import AdminStates
    await state.set_state(AdminStates.waiting_ban_duration)
    await callback.answer()


# ──────────────────────────────────────────────
# Callback: Удаление уведомления из базы
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("ntf_delete:"))
async def process_ntf_delete(callback: CallbackQuery, state: FSMContext):
    ntf_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.id == ntf_id))
        ntf = result.scalar_one_or_none()
        if not ntf or ntf.receiver_id != callback.from_user.id:
            await callback.answer("Уведомление не найдено.", show_alert=True)
            return
        await session.execute(sa_delete(Notification).where(Notification.id == ntf_id))
        await session.commit()

    await callback.answer("🗑 Уведомление удалено.")
    await process_ntf_inbox(callback, state)


# ──────────────────────────────────────────────
# Callback: Закрыть панель уведомлений
# ──────────────────────────────────────────────

@router.callback_query(F.data == "ntf_close")
async def process_ntf_close(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    for key in ("notification_panel_msg_id", "notification_sub_msg_id"):
        if data.get(key):
            await safe_delete(callback.bot, callback.message.chat.id, data[key])
    await state.update_data(notification_panel_msg_id=None, notification_sub_msg_id=None)

    # Стираем пересланные сообщения
    fwd = data.get("notification_fwd_msg_id")
    if fwd:
        if isinstance(fwd, list):
            for m_id in fwd:
                await safe_delete(callback.bot, callback.message.chat.id, m_id)
        else:
            await safe_delete(callback.bot, callback.message.chat.id, fwd)
        await state.update_data(notification_fwd_msg_id=None)

    await callback.answer()


@router.callback_query(F.data == "ntf_back_panel")
async def process_ntf_back_panel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()

    # Стираем пересланные сообщения
    fwd = data.get("notification_fwd_msg_id")
    if fwd:
        if isinstance(fwd, list):
            for m_id in fwd:
                await safe_delete(callback.bot, callback.message.chat.id, m_id)
        else:
            await safe_delete(callback.bot, callback.message.chat.id, fwd)
        await state.update_data(notification_fwd_msg_id=None)

    await show_notifications_panel(callback.bot, callback.message.chat.id, callback.from_user.id, state)


# ──────────────────────────────────────────────
# Direct Alerts: Чтение из чата («📖 Прочитать»)
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("ntf_read_alert:"))
async def process_ntf_read_alert(callback: CallbackQuery, state: FSMContext):
    ntf_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.id == ntf_id))
        ntf = result.scalar_one_or_none()

        if not ntf:
            await callback.answer("Уведомление удалено.", show_alert=True)
            await safe_delete(callback.bot, callback.message.chat.id, callback.message.message_id)
            return

        # Помечаем прочитанным
        if not ntf.is_read:
            ntf.is_read = True
            ntf.read_at = utcnow()
            await session.commit()

    # Удаляем сообщение алерта
    await safe_delete(callback.bot, callback.message.chat.id, callback.message.message_id)

    # Меняем callback data и делегируем отображение в process_ntf_view
    callback.data = f"ntf_view:{ntf_id}"
    await process_ntf_view(callback, state)


# ──────────────────────────────────────────────
# Callback: Взять жалобу в работу
# ──────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm_take_ntf:"))
async def process_adm_take_ntf(callback: CallbackQuery, state: FSMContext):
    ntf_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Notification).where(Notification.id == ntf_id))
        ntf = result.scalar_one_or_none()
        if not ntf:
            await callback.answer("Уведомление не найдено.", show_alert=True)
            return

        if ntf.accepted_by:
            await callback.answer("Эта жалоба уже взята в работу другим модератором.", show_alert=True)
            return

        # Обновляем все уведомления с этой жалобой для этого поста
        if ntf.post_id:
            await session.execute(
                update(Notification)
                .where(
                    Notification.post_id == ntf.post_id,
                    Notification.channel_id == ntf.channel_id
                )
                .values(accepted_by=callback.from_user.id)
            )
            await session.commit()
            await callback.answer("✅ Вы взяли жалобу в работу.")
        else:
            ntf.accepted_by = callback.from_user.id
            await session.commit()
            await callback.answer("✅ Взято в работу.")

    # Обновляем просмотр
    await process_ntf_view(callback, state)


@router.callback_query(F.data == "ntf_close_alert")
async def process_ntf_close_alert(callback: CallbackQuery):
    await safe_delete(callback.bot, callback.message.chat.id, callback.message.message_id)
    await callback.answer()


# ──────────────────────────────────────────────
# Отправка уведомлений: Выбор категории
# ──────────────────────────────────────────────

@router.callback_query(F.data == "ntf_send_choose")
async def process_ntf_send_choose(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")

    async with AsyncSessionLocal() as session:
        role = await get_user_role(session, callback.from_user.id, channel_id)

    buttons = []
    
    if role == "superadmin":
        # Супер-админ может слать всем или конкретному
        buttons.append([InlineKeyboardButton(text="👥 Всем пользователям", callback_data="ntf_target:all")])
        buttons.append([InlineKeyboardButton(text="👤 Конкретному пользователю", callback_data="ntf_target:user")])
    elif role == "moderator":
        # Модератор может слать админу или конкретному
        buttons.append([InlineKeyboardButton(text="👑 Владельцу канала (Super Admin)", callback_data="ntf_target:owner")])
        buttons.append([InlineKeyboardButton(text="👤 Конкретному пользователю", callback_data="ntf_target:user")])
    else:
        # Обычный пользователь может слать админу, модераторам или конкретному
        buttons.append([InlineKeyboardButton(text="👑 Владельцу канала", callback_data="ntf_target:owner")])
        buttons.append([InlineKeyboardButton(text="🛡 Модераторам канала", callback_data="ntf_target:mods")])
        buttons.append([InlineKeyboardButton(text="👤 Другому пользователю (по внутр. ID)", callback_data="ntf_target:user")])

    buttons.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="ntf_back_panel")])

    if data.get("notification_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["notification_sub_msg_id"])

    sent = await callback.message.answer(
        "📤 <b>Отправка уведомления</b>\n\nВыберите получателя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(notification_sub_msg_id=sent.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("ntf_target:"))
async def process_ntf_target(callback: CallbackQuery, state: FSMContext):
    target_type = callback.data.split(":")[1]
    await state.update_data(ntf_target_type=target_type)
    
    data = await state.get_data()
    if data.get("notification_panel_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["notification_panel_msg_id"])
    if data.get("notification_sub_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["notification_sub_msg_id"])

    await state.update_data(notification_panel_msg_id=None, notification_sub_msg_id=None)

    if target_type == "user":
        # Спрашиваем ID
        await state.set_state(NotificationStates.waiting_target_user)
        prompt = await callback.message.answer(
            "👤 <b>Введите получателя</b>\n\nОтправьте 9-значный внутренний ID получателя в этом канале:",
            reply_markup=back_only_keyboard(),
            parse_mode="HTML"
        )
        await state.update_data(prompt_msg_id=prompt.message_id)
    else:
        # Спрашиваем текст
        await state.set_state(NotificationStates.waiting_text)
        labels = {
            "all": "всем пользователям бота",
            "owner": "владельцу канала",
            "mods": "модераторам канала"
        }
        prompt = await callback.message.answer(
            f"📝 <b>Введите текст уведомления</b> для {labels.get(target_type)}:\n"
            f"<i>(Разрешено HTML-форматирование)</i>",
            reply_markup=back_only_keyboard(),
            parse_mode="HTML"
        )
        await state.update_data(prompt_msg_id=prompt.message_id)
        
    await callback.answer()


# ──────────────────────────────────────────────
# FSM: Ввод ID пользователя
# ──────────────────────────────────────────────

@router.message(NotificationStates.waiting_target_user)
async def process_ntf_input_target(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    target_input = message.text.strip() if message.text else ""

    await safe_delete(message.bot, message.chat.id, message.message_id)

    if not target_input or len(target_input) != 9:
        await notify(message, "⚠️ Введите корректный 9-значный внутренний ID (например, 000000002).")
        return

    target_user_id = None
    async with AsyncSessionLocal() as session:
        # Поиск только по internal_id
        cu_result = await session.execute(
            select(ChannelUser.user_id)
            .where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.internal_id == target_input
            )
        )
        target_user_id = cu_result.scalar_one_or_none()
        
        if target_user_id:
            user_res = await session.execute(select(User).where(User.user_id == target_user_id))
            target_user = user_res.scalar_one_or_none()
        else:
            target_user = None

    if not target_user:
        await notify(message, "❌ Пользователь с таким внутренним ID не найден в этом канале.")
        return

    # Запоминаем получателя
    await state.update_data(ntf_target_user_id=target_user_id)

    if data.get("prompt_msg_id"):
        await safe_delete(message.bot, message.chat.id, data["prompt_msg_id"])

    await state.set_state(NotificationStates.waiting_text)
    name_str = f"@{target_user.username}" if target_user.username else target_user.full_name
    prompt = await message.answer(
        f"📝 <b>Введите текст уведомления</b> для пользователя <b>{name_str}</b> (ID: <code>{target_input}</code>):\n"
        f"<i>(Разрешено HTML-форматирование)</i>",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


# ──────────────────────────────────────────────
# FSM: Ввод текста уведомления и отправка
# ──────────────────────────────────────────────

@router.message(NotificationStates.waiting_text)
async def process_ntf_input_text(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    target_type = data.get("ntf_target_type")
    text_content = message.text.strip() if message.text else ""

    await safe_delete(message.bot, message.chat.id, message.message_id)

    if not text_content:
        await notify(message, "⚠️ Сообщение не может быть пустым.")
        return

    async with AsyncSessionLocal() as session:
        # Ищем отправителя
        cu_sender = await session.execute(
            select(ChannelUser.internal_id).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.user_id == message.from_user.id
            )
        )
        sender_internal_id = cu_sender.scalar_one_or_none() or "—"
        
        # Получаем список получателей в зависимости от типа
        recipients = []
        
        if target_type == "all":
            # Только для superadmin
            role = await get_user_role(session, message.from_user.id, channel_id)
            if role != "superadmin":
                await notify(message, "❌ Рассылка всем доступна только Супер-администратору.")
                await state.set_state(None)
                await show_notifications_panel(message.bot, message.chat.id, message.from_user.id, state)
                return
            
            res_all = await session.execute(select(User.user_id))
            recipients = [row[0] for row in res_all.all()]
            
        elif target_type == "owner":
            # Владелец канала
            ch_res = await session.execute(select(Channel.owner_id).where(Channel.channel_id == channel_id))
            owner_id = ch_res.scalar_one_or_none()
            if owner_id:
                recipients = [owner_id]
                
        elif target_type == "mods":
            # Модераторы канала (и superadmin)
            mods_res = await session.execute(
                select(ChannelUser.user_id).where(
                    ChannelUser.channel_id == channel_id,
                    ChannelUser.role.in_(("moderator", "superadmin"))
                )
            )
            recipients = [row[0] for row in mods_res.all()]
            
        elif target_type == "user":
            # Конкретный пользователь
            target_user_id = data.get("ntf_target_user_id")
            if target_user_id:
                recipients = [target_user_id]

        if not recipients:
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, "⚠️ Получатели не найдены.")
            await state.set_state(None)
            await show_notifications_panel(message.bot, message.chat.id, message.from_user.id, state)
            return

        # Форматируем текст в соответствии с типом рассылки
        if target_type == "all":
            formatted_text = f"📢 <b>Объявление от администрации:</b>\n\n{text_content}"
        elif target_type == "owner":
            formatted_text = f"✉️ <b>Обращение к владельцу канала:</b>\n\n{text_content}"
        elif target_type == "mods":
            formatted_text = f"✉️ <b>Обращение к модераторам канала:</b>\n\n{text_content}"
        else:
            formatted_text = f"✉️ <b>Личное уведомление:</b>\n\n{text_content}"

        # Записываем в базу уведомлений для каждого
        ntfs_to_add = []
        for r_id in recipients:
            ntfs_to_add.append(Notification(
                sender_id=message.from_user.id,
                receiver_id=r_id,
                channel_id=channel_id,
                text=formatted_text,
                is_read=False,
                created_at=utcnow()
            ))
        session.add_all(ntfs_to_add)
        await session.commit()

        # Получаем сгенерированные id
        ntf_map = {n.receiver_id: n.id for n in ntfs_to_add}

    # Удаляем подсказку о вводе
    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await state.update_data(prompt_msg_id=None, ntf_target_user_id=None, ntf_target_type=None)

    # Рассылаем алерты получателям в фоновом режиме
    async def send_alerts(bot_inst, target_ids, id_map):
        success = 0
        for uid in target_ids:
            try:
                nid = id_map.get(uid)
                if not nid:
                    continue
                
                # Отправляем сообщение-алерт с кнопкой
                keyboard = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📖 Прочитать", callback_data=f"ntf_read_alert:{nid}")]
                ])
                await bot_inst.send_message(
                    chat_id=uid,
                    text="🔔 <b>У вас новое уведомление!</b>",
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                success += 1
                await asyncio.sleep(0.05) # Лимиты Telegram
            except Exception:
                pass
        
        # Если это была массовая рассылка superadmin
        if target_type == "all" and len(target_ids) > 1:
            try:
                await bot_inst.send_message(
                    chat_id=message.from_user.id,
                    text=f"📢 <b>Рассылка завершена!</b>\nОтправлено алертов: <code>{success}/{len(target_ids)}</code>",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    import asyncio
    asyncio.create_task(send_alerts(message.bot, recipients, ntf_map))

    await notify(message, "🚀 Уведомление отправлено!")
    await show_notifications_panel(message.bot, message.chat.id, message.from_user.id, state)
