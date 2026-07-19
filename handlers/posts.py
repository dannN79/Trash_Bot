import re
import asyncio
import json
import weakref
from datetime import datetime, timezone as pytimezone, timedelta
from zoneinfo import ZoneInfo

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto,
)
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import AiogramError
from sqlalchemy import select, func

from db.database import AsyncSessionLocal
from db.models import Post, ChannelUser, Channel, Ban, Notification
from states.states import NewPost, EditPost, ReportPost, NotificationStates
from keyboards.reply import main_menu, posts_menu, back_only_keyboard, draft_keyboard
from utils import (
    safe_delete, notify, delete_pair, edit_or_resend,
    utc_to_local, get_channel_timezone, is_user_banned,
    cleanup_chat, utcnow, get_user_role, get_channel_title,
)

router = Router()

DAILY_POST_LIMIT = 2
DAILY_EDIT_LIMIT = 2
PAGE_SIZE = 10


# ══════════════════════════════════════════════════════════
# Вспомогательные функции
# ══════════════════════════════════════════════════════════

async def get_active_channel(message_or_cb, state: FSMContext) -> int | None:
    """Возвращает active_channel_id из FSM. Если нет — пытается авто-выбрать."""
    data = await state.get_data()
    chan_id = data.get("active_channel_id")
    if chan_id:
        return chan_id

    user_id = (message_or_cb.from_user.id
               if hasattr(message_or_cb, 'from_user')
               else message_or_cb.message.from_user.id)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChannelUser.channel_id).where(
                ChannelUser.user_id == user_id
            )
        )
        rows = result.fetchall()

    if len(rows) == 1:
        chan_id = rows[0][0]
        await state.update_data(active_channel_id=chan_id)
        return chan_id
    return None


async def show_main_menu(bot, chat_id: int, user_id: int, state: FSMContext):
    """Показывает главное меню, очищая все предыдущие сообщения."""
    from handlers.start import show_main_menu_msg
    await show_main_menu_msg(bot, chat_id, user_id, state)


async def enter_posts_section(bot, chat_id: int, user_id: int, state: FSMContext):
    """Входит в раздел публикаций."""
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    if not channel_id:
        return

    # Очищаем предыдущие сообщения бота
    await cleanup_chat(bot, chat_id, state)

    # Показываем список постов
    has_posts = await show_posts_list(bot, chat_id, user_id, channel_id, state)

    if not has_posts:
        empty = await bot.send_message(
            chat_id,
            "📭 У вас пока нет публикаций в этом канале.\n"
            "Нажмите <b>📝 Новая публикация</b> чтобы создать первую!",
            parse_mode="HTML"
        )
        await state.update_data(posts_list_msg_id=empty.message_id)

    # Отправляем reply-клавиатуру раздела
    kb_msg = await bot.send_message(
        chat_id,
        "📋 <b>Мои публикации</b>",
        reply_markup=posts_menu(),
        parse_mode="HTML"
    )
    await state.update_data(posts_kb_msg_id=kb_msg.message_id)


# ══════════════════════════════════════════════════════════
# Пагинация постов
# ══════════════════════════════════════════════════════════

def build_posts_keyboard(posts: list, page: int, total: int, tz_name: str) -> InlineKeyboardMarkup:
    rows = []
    for post in posts:
        local_dt = utc_to_local(post.created_at, tz_name)
        date_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"
        media_icons = {
            "photo": "🖼", "video": "🎬", "document": "📎",
            "animation": "🎞", "text": "📝", "album": "🖼"
        }
        icon = media_icons.get(post.media_type, "📝")
        preview = ""
        if post.text:
            preview = post.text[:30].replace("\n", " ")
            if len(post.text) > 30:
                preview += "…"
        rows.append([InlineKeyboardButton(
            text=f"{icon} {date_str}  {preview}",
            callback_data=f"vp:{post.id}"
        )])

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pp:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pp:{page + 1}"))
    if nav:
        rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_posts_list(
    bot, chat_id: int, user_id: int,
    channel_id: int, state: FSMContext, page: int = 0,
) -> bool:
    """Показывает пагинированный список постов. Возвращает True если посты есть."""
    async with AsyncSessionLocal() as session:
        tz_name = await get_channel_timezone(session, channel_id)
        total_result = await session.execute(
            select(func.count()).where(
                Post.user_id == user_id,
                Post.channel_id == channel_id,
                Post.is_deleted == False
            )
        )
        total = total_result.scalar() or 0
        if total == 0:
            return False

        result = await session.execute(
            select(Post)
            .where(Post.user_id == user_id, Post.channel_id == channel_id, Post.is_deleted == False)
            .order_by(Post.created_at.desc())
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
        posts = result.scalars().all()

    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    keyboard = build_posts_keyboard(posts, page, total, tz_name)
    text = (
        f"📋 <b>Ваши публикации</b>\n"
        f"Всего: {total}  |  Страница {page + 1} из {total_pages}"
    )

    data = await state.get_data()
    new_id = await edit_or_resend(
        bot=bot,
        chat_id=chat_id,
        old_message_id=data.get("posts_list_msg_id"),
        text=text,
        reply_markup=keyboard,
    )
    await state.update_data(posts_list_msg_id=new_id)
    return True


# ══════════════════════════════════════════════════════════
# Callback: пагинация / просмотр поста
# ══════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("pp:"))
async def process_posts_page(callback: CallbackQuery, state: FSMContext):
    page = int(callback.data.split(":")[1])
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    if not channel_id:
        await callback.answer("Сначала выберите канал.", show_alert=True)
        return
    await show_posts_list(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state, page=page
    )
    await callback.answer()


@router.callback_query(F.data.startswith("vp:"))
async def process_view_post(callback: CallbackQuery, state: FSMContext):
    """Просмотр поста — пересылает из канала, если найден."""
    post_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await callback.answer("Пост не найден или удалён.", show_alert=True)
            return
        role = await get_user_role(session, callback.from_user.id, post.channel_id)
        tz_name = await get_channel_timezone(session, post.channel_id)

    is_owner = post.user_id == callback.from_user.id
    can_edit = is_owner or role in ("moderator", "superadmin")

    await state.update_data(
        selected_post_id=post.id,
        selected_post_channel_id=post.channel_id,
    )

    # Закрываем старую карточку если была
    data = await state.get_data()
    if data.get("post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["post_card_msg_id"])
    if data.get("forwarded_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["forwarded_msg_id"])

    # Пытаемся переслать пост из канала
    forwarded_msg_id = None
    post_found_in_channel = True
    
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
    except Exception:
        post_found_in_channel = False

    local_dt = utc_to_local(post.created_at, tz_name)
    dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

    media_labels = {
        "photo": "🖼 Фото", "video": "🎬 Видео", "document": "📎 Документ",
        "animation": "🎞 GIF", "text": "📝 Текст", "album": "🖼 Альбом"
    }

    if post_found_in_channel:
        # Пост найден — показываем управление
        buttons = []
        if can_edit:
            buttons.append([
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="edit_from_view"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data="del_from_view"),
            ])
        buttons.append([
            InlineKeyboardButton(text="✖️ Закрыть", callback_data="close_card")
        ])

        card_text = (
            f"⚙️ <b>Публикация от {dt_str}</b>\n"
            f"🕒 Тип: {media_labels.get(post.media_type, '📝 Текст')}"
        )
    else:
        # Пост НЕ найден в канале — предлагаем удалить из списка
        buttons = [
            [InlineKeyboardButton(
                text="🗑 Удалить из моего списка",
                callback_data=f"remove_ghost:{post.id}"
            )],
            [InlineKeyboardButton(text="✖️ Закрыть", callback_data="close_card")]
        ]
        card_text = (
            f"⚠️ <b>Публикация от {dt_str}</b>\n\n"
            f"❌ <i>Публикация не найдена в канале — возможно, была удалена вручную.</i>"
        )

    sent = await callback.message.answer(
        card_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="HTML"
    )
    await state.update_data(
        post_card_msg_id=sent.message_id,
        forwarded_msg_id=forwarded_msg_id,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("remove_ghost:"))
async def process_remove_ghost_post(callback: CallbackQuery, state: FSMContext):
    """Удаляет «призрачный» пост из списка пользователя."""
    post_id = int(callback.data.split(":")[1])

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if post:
            post.is_deleted = True
            post.deleted_by = callback.from_user.id
            post.deleted_at = utcnow()
            post.delete_reason = "Публикация удалена из канала, запись скрыта пользователем"
            await session.commit()

    # Закрываем карточку
    data = await state.get_data()
    if data.get("post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["post_card_msg_id"])
    if data.get("forwarded_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["forwarded_msg_id"])

    await state.update_data(
        post_card_msg_id=None, forwarded_msg_id=None,
        selected_post_id=None,
    )
    await callback.answer("🗑 Пост удалён из списка.")

    channel_id = data.get("active_channel_id")
    has_posts = await show_posts_list(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state
    )
    if not has_posts:
        old_list = (await state.get_data()).get("posts_list_msg_id")
        if old_list:
            await safe_delete(callback.bot, callback.message.chat.id, old_list)
        empty = await callback.bot.send_message(
            callback.message.chat.id, "📭 Публикаций больше нет."
        )
        await state.update_data(posts_list_msg_id=empty.message_id)


@router.callback_query(F.data == "close_card")
async def close_post_card(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["post_card_msg_id"])
    if data.get("forwarded_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["forwarded_msg_id"])
    await state.update_data(
        post_card_msg_id=None, forwarded_msg_id=None, selected_post_id=None
    )
    await callback.answer()


@router.callback_query(F.data == "edit_from_view")
async def process_edit_from_view(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_id = data.get("selected_post_id")
    if not post_id:
        await callback.answer("Ошибка. Начните сначала.", show_alert=True)
        return

    # Проверяем лимит редактирования для обычных пользователей
    async with AsyncSessionLocal() as session:
        post_result = await session.execute(select(Post).where(Post.id == post_id))
        post = post_result.scalar_one_or_none()
        if not post:
            await callback.answer("Пост не найден.", show_alert=True)
            return

        role = await get_user_role(session, callback.from_user.id, post.channel_id)
        if role == "user":
            cu_result = await session.execute(
                select(ChannelUser).where(
                    ChannelUser.user_id == callback.from_user.id,
                    ChannelUser.channel_id == post.channel_id,
                )
            )
            cu = cu_result.scalar_one_or_none()
            if cu:
                tz_name = await get_channel_timezone(session, post.channel_id)
                tz = ZoneInfo(tz_name)
                local_now = datetime.now(tz)
                local_today = local_now.date()

                # Сброс счётчика если новый день
                if cu.edits_today_date:
                    last_edit_local = utc_to_local(cu.edits_today_date, tz_name)
                    if last_edit_local.date() < local_today:
                        cu.edits_today = 0
                        cu.edits_today_date = utcnow()
                        await session.commit()
                else:
                    cu.edits_today = 0
                    cu.edits_today_date = utcnow()
                    await session.commit()

                if cu.edits_today >= DAILY_EDIT_LIMIT:
                    await callback.answer(
                        f"⛔️ Лимит: {DAILY_EDIT_LIMIT} редактирования в день.",
                        show_alert=True
                    )
                    return

    # Закрываем карточку
    if data.get("post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["post_card_msg_id"])
    if data.get("forwarded_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["forwarded_msg_id"])

    await state.set_state(EditPost.waiting_new_content)
    prompt = await callback.message.answer(
        "✏️ <b>Редактирование публикации</b>\n\n"
        "Отправьте новый контент (текст, фото, видео или документ).\n"
        "Старая публикация будет заменена.",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(
        prompt_msg_id=prompt.message_id,
        post_card_msg_id=None,
        forwarded_msg_id=None,
    )
    await callback.answer()


@router.callback_query(F.data == "del_from_view")
async def process_delete_from_view(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    post_id = data.get("selected_post_id")
    channel_id = data.get("selected_post_channel_id") or data.get("active_channel_id")

    if not post_id or not channel_id:
        await callback.answer("Ошибка состояния.", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await callback.answer("Пост не найден.", show_alert=True)
            return

        role = await get_user_role(session, callback.from_user.id, post.channel_id)
        if post.user_id != callback.from_user.id and role not in ("moderator", "superadmin"):
            await callback.answer("У вас нет прав на удаление.", show_alert=True)
            return

        # Удаляем из канала
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
                await callback.bot.delete_message(
                    chat_id=channel_id, message_id=msg_id
                )
            except Exception:
                pass

        # Мягкое удаление
        post.is_deleted = True
        post.deleted_by = callback.from_user.id
        post.deleted_at = utcnow()

        if post.user_id != callback.from_user.id and role in ("moderator", "superadmin"):
            post.delete_reason = "Удалено модератором/админом"
            # Счётчик нарушений
            cu_result = await session.execute(
                select(ChannelUser).where(
                    ChannelUser.user_id == post.user_id,
                    ChannelUser.channel_id == channel_id
                )
            )
            cu = cu_result.scalar_one_or_none()
            if cu:
                cu.deleted_by_admin_count = (cu.deleted_by_admin_count or 0) + 1
                tz_name = await get_channel_timezone(session, channel_id)
                local_created = utc_to_local(post.created_at, tz_name)
                created_str = local_created.strftime("%d.%m.%Y %H:%M") if local_created else "—"

                if cu.deleted_by_admin_count >= 3:
                    ban_until = utcnow() + timedelta(days=14)
                    session.add(Ban(
                        channel_id=channel_id,
                        user_id=post.user_id,
                        banned_by=callback.from_user.id,
                        ban_until=ban_until,
                        reason="Автоматический бан: 3 нарушения"
                    ))
                    local_until = utc_to_local(ban_until, tz_name)
                    ban_str = local_until.strftime("%d.%m.%Y %H:%M")
                    ntf_text = f"🚫 <b>Автобан на 14 дней (до {ban_str})</b> за 3 нарушения.\nУдалена публикация от {created_str}.\nПричина последнего удаления: <i>Удалено модератором/админом</i>"
                else:
                    ntf_text = f"⚠️ <b>Ваша публикация от {created_str} была удалена модератором.</b>\nПричина: <i>Удалено модератором/админом</i>\nНарушений: {cu.deleted_by_admin_count}/3"
                
                # Создаем и отправляем уведомление
                ntf = Notification(
                    sender_id=callback.from_user.id,
                    receiver_id=post.user_id,
                    channel_id=channel_id,
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
                    await callback.bot.send_message(
                        chat_id=post.user_id,
                        text="🔔 <b>У вас новое уведомление!</b>",
                        reply_markup=keyboard,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        else:
            post.delete_reason = "Удалено автором"

        await session.commit()

    # Очищаем карточку
    if data.get("post_card_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["post_card_msg_id"])
    if data.get("forwarded_msg_id"):
        await safe_delete(callback.bot, callback.message.chat.id, data["forwarded_msg_id"])

    await state.update_data(
        selected_post_id=None, post_card_msg_id=None,
        forwarded_msg_id=None,
    )
    await callback.answer("🗑 Пост удалён.")

    # Обновляем список
    has_posts = await show_posts_list(
        callback.bot, callback.message.chat.id,
        callback.from_user.id, channel_id, state
    )
    if not has_posts:
        old_list = (await state.get_data()).get("posts_list_msg_id")
        if old_list:
            await safe_delete(callback.bot, callback.message.chat.id, old_list)
        empty = await callback.bot.send_message(
            callback.message.chat.id, "📭 Публикаций больше нет."
        )
        await state.update_data(posts_list_msg_id=empty.message_id)


# ══════════════════════════════════════════════════════════
# Reply-меню
# ══════════════════════════════════════════════════════════

@router.message(F.text == "📋 Мои публикации")
async def show_posts_section(message: Message, state: FSMContext):
    await state.set_state(None)
    await safe_delete(message.bot, message.chat.id, message.message_id)

    channel_id = await get_active_channel(message, state)
    if not channel_id:
        await notify(message, "⚠️ Сначала подключите канал через «🔄 Сменить канал».", delay=5)
        return

    await enter_posts_section(
        message.bot, message.chat.id, message.from_user.id, state
    )


@router.message(F.text == "📝 Новая публикация")
async def new_post_start(message: Message, state: FSMContext):
    await safe_delete(message.bot, message.chat.id, message.message_id)

    channel_id = await get_active_channel(message, state)
    if not channel_id:
        await notify(message, "⚠️ Сначала подключите канал.", delay=4)
        return

    async with AsyncSessionLocal() as session:
        # Проверка бана
        is_banned, ban_until, ban_reason = await is_user_banned(
            session, message.from_user.id, channel_id
        )
        if is_banned:
            tz = await get_channel_timezone(session, channel_id)
            local_ban = utc_to_local(ban_until, tz)
            ban_str = local_ban.strftime("%d.%m.%Y %H:%M")
            await notify(
                message,
                f"🚫 <b>Вы забанены!</b>\n"
                f"До: <code>{ban_str}</code>\n"
                f"Причина: <i>{ban_reason or 'не указана'}</i>",
                delay=6
            )
            return

        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == message.from_user.id,
                ChannelUser.channel_id == channel_id,
            )
        )
        cu = cu_result.scalar_one_or_none()

        if cu and cu.role == "user":
            tz_name = await get_channel_timezone(session, channel_id)
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            local_now = datetime.now(tz)
            local_today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_today_start = local_today_start.astimezone(pytimezone.utc).replace(tzinfo=None)

            posts_count_result = await session.execute(
                select(func.count(Post.id)).where(
                    Post.user_id == message.from_user.id,
                    Post.channel_id == channel_id,
                    Post.created_at >= utc_today_start,
                    Post.is_deleted == False
                )
            )
            posts_today = posts_count_result.scalar() or 0

            if posts_today >= DAILY_POST_LIMIT:
                await notify(
                    message,
                    f"⛔️ Лимит: {DAILY_POST_LIMIT} публикации в день.\n"
                    "Попробуйте завтра.",
                    delay=5
                )
                return

    await state.set_state(NewPost.waiting_content)
    # Инициализируем пустой черновик
    await state.update_data(
        post_text=None,
        post_photos=[],
        prompt_msg_id=None
    )
    prompt = await message.answer(
        "📝 <b>Создание новой публикации</b>\n\n"
        "Вы можете отправить текст и одну или несколько фотографий по очереди.\n\n"
        "<b>Текущий черновик:</b>\n"
        "💬 Текст: <i>не отправлен</i>\n"
        "🖼 Фотографий: <code>0</code>\n\n"
        "Вы можете прислать текст или фото. Нажмите <b>✅ Готово</b> для отправки.",
        reply_markup=draft_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


@router.message(F.text == "🔙 Назад")
async def cancel_action(message: Message, state: FSMContext):
    await safe_delete(message.bot, message.chat.id, message.message_id)

    data = await state.get_data()
    if data.get("prompt_msg_id"):
        await safe_delete(message.bot, message.chat.id, data["prompt_msg_id"])
        await state.update_data(prompt_msg_id=None)

    current = await state.get_state()
    await state.set_state(None)

    if current and current.startswith("NotificationStates"):
        await notify(message, "↩️ Действие отменено.", delay=2)
        from handlers.notifications import show_notifications_panel
        await show_notifications_panel(message.bot, message.chat.id, message.from_user.id, state)
        return

    if current and current.startswith("AdminStates"):
        # Возвращаемся в админку
        channel_id = data.get("active_channel_id")
        if channel_id:
            from handlers.admin import enter_admin_panel
            await enter_admin_panel(
                message.bot, message.chat.id,
                message.from_user.id, channel_id, state
            )
        else:
            await notify(message, "↩️ Действие отменено.", delay=2)
        return

    if current in (
        NewPost.waiting_content,
        EditPost.waiting_new_content,
    ):
        await notify(message, "↩️ Действие отменено.", delay=2)
        await enter_posts_section(
            message.bot, message.chat.id, message.from_user.id, state
        )
    elif current in (
        ReportPost.waiting_post_reference,
        ReportPost.waiting_reason,
    ):
        await notify(message, "↩️ Действие отменено.", delay=2)
        await show_main_menu(
            message.bot, message.chat.id, message.from_user.id, state
        )
    else:
        await show_main_menu(
            message.bot, message.chat.id, message.from_user.id, state
        )


@router.message(F.text == "🔙 Назад в главное меню")
async def back_to_main_menu(message: Message, state: FSMContext):
    await safe_delete(message.bot, message.chat.id, message.message_id)
    await state.set_state(None)
    await show_main_menu(
        message.bot, message.chat.id, message.from_user.id, state
    )


# ══════════════════════════════════════════════════════════
# FSM: Создание публикации
# ══════════════════════════════════════════════════════════

def extract_content(message: Message) -> tuple[str, str | None, str | None]:
    """
    Извлекает контент из сообщения.
    Возвращает: (media_type, file_id, text_or_caption)
    """
    if message.photo:
        return "photo", message.photo[-1].file_id, message.caption
    elif message.video:
        return "video", message.video.file_id, message.caption
    elif message.document:
        return "document", message.document.file_id, message.caption
    elif message.animation:
        return "animation", message.animation.file_id, message.caption
    elif message.text:
        return "text", None, message.text.strip()
    return "unknown", None, None


async def send_to_channel(bot, channel_id: int, media_type: str,
                          file_id: str | None, text: str, signature: str) -> Message:
    """Отправляет публикацию в канал с подписью пользователя."""
    if media_type == "text":
        full_text = f"{text}\n\n<i>{signature}</i>"
        return await bot.send_message(
            chat_id=channel_id, text=full_text, parse_mode="HTML"
        )
    else:
        caption = f"{text}\n\n<i>{signature}</i>" if text else f"<i>{signature}</i>"
        send_method = {
            "photo": bot.send_photo,
            "video": bot.send_video,
            "document": bot.send_document,
            "animation": bot.send_animation,
        }
        method = send_method.get(media_type, bot.send_document)
        kwargs = {
            "chat_id": channel_id,
            media_type: file_id,
            "caption": caption,
            "parse_mode": "HTML",
        }
        return await method(**kwargs)


async def publish_draft(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    if not channel_id:
        await notify(message, "Сначала выберите канал.")
        return

    post_text = data.get("post_text")
    post_photos = data.get("post_photos", [])

    if not post_text and not post_photos:
        await notify(message, "⚠️ Ваш черновик пуст. Отправьте текст или фото перед нажатием ✅ Готово.")
        return

    async with AsyncSessionLocal() as session:
        # Проверка бана
        is_banned, _, _ = await is_user_banned(session, message.from_user.id, channel_id)
        if is_banned:
            await state.set_state(None)
            await notify(message, "🚫 Вы забанены в этом канале.")
            return

        cu_result = await session.execute(
            select(ChannelUser).where(
                ChannelUser.user_id == message.from_user.id,
                ChannelUser.channel_id == channel_id,
            )
        )
        cu = cu_result.scalar_one_or_none()
        if not cu:
            await notify(message, "Вы не зарегистрированы в этом канале.")
            await state.set_state(None)
            return

        # Проверка лимита
        if cu.role == "user":
            tz_name = await get_channel_timezone(session, channel_id)
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
            local_now = datetime.now(tz)
            local_today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            utc_today_start = local_today_start.astimezone(pytimezone.utc).replace(tzinfo=None)

            posts_count_result = await session.execute(
                select(func.count(Post.id)).where(
                    Post.user_id == message.from_user.id,
                    Post.channel_id == channel_id,
                    Post.created_at >= utc_today_start,
                    Post.is_deleted == False
                )
            )
            posts_today = posts_count_result.scalar() or 0
            if posts_today >= DAILY_POST_LIMIT:
                await notify(message, f"⛔️ Лимит {DAILY_POST_LIMIT} публикации в день превышен.")
                await state.set_state(None)
                return

        # Номер поста (БЕЗ фильтра is_deleted, чтобы не дублировать)
        max_num = await session.execute(
            select(func.max(Post.post_number)).where(
                Post.user_id == message.from_user.id,
                Post.channel_id == channel_id,
            )
        )
        next_num = (max_num.scalar() or 0) + 1

        # Подпись: internal_id пользователя
        signature = f"Опубликовано: {cu.internal_id}"

        # Публикуем в канал
        try:
            # Сценарии публикации
            if len(post_photos) > 1:
                # Альбом (несколько фото)
                caption = f"{post_text}\n\n<i>{signature}</i>" if post_text else f"<i>{signature}</i>"
                media = []
                for i, fid in enumerate(post_photos):
                    if i == 0:
                        media.append(InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML"))
                    else:
                        media.append(InputMediaPhoto(media=fid))
                
                sent_msgs = await message.bot.send_media_group(chat_id=channel_id, media=media)
                message_ids = [m.message_id for m in sent_msgs]
                main_msg_id = message_ids[0]
                media_type = "album"
                file_ids_db = json.dumps({"file_ids": post_photos, "message_ids": message_ids})
            elif len(post_photos) == 1:
                # Одиночное фото
                caption = f"{post_text}\n\n<i>{signature}</i>" if post_text else f"<i>{signature}</i>"
                sent = await message.bot.send_photo(
                    chat_id=channel_id,
                    photo=post_photos[0],
                    caption=caption,
                    parse_mode="HTML"
                )
                main_msg_id = sent.message_id
                media_type = "photo"
                file_ids_db = post_photos[0]
            else:
                # Только текст
                full_text = f"{post_text}\n\n<i>{signature}</i>"
                sent = await message.bot.send_message(
                    chat_id=channel_id,
                    text=full_text,
                    parse_mode="HTML"
                )
                main_msg_id = sent.message_id
                media_type = "text"
                file_ids_db = None

        except AiogramError as e:
            await notify(message, f"❌ Не удалось опубликовать: {e}", delay=5)
            await state.set_state(None)
            return

        session.add(Post(
            post_number=next_num,
            channel_id=channel_id,
            user_id=message.from_user.id,
            message_id=main_msg_id,
            text=post_text or "",
            media_type=media_type,
            file_ids=file_ids_db,
            created_at=utcnow(),
        ))

        cu.last_post_time = utcnow()
        await session.commit()

    # Удаляем сообщение-подсказку
    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await state.update_data(prompt_msg_id=None)

    await notify(message, "✅ Пост опубликован!", delay=3)

    # Обновляем список
    await enter_posts_section(
        message.bot, message.chat.id,
        message.from_user.id, state
    )


# Слабые ссылки: лок GC-нится автоматически когда никто не держит черновик
_user_draft_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


@router.message(NewPost.waiting_content)
async def save_post(message: Message, state: FSMContext):
    # Если нажата кнопка "Готово"
    if message.text == "✅ Готово":
        await publish_draft(message, state)
        return

    # Удаляем сообщение пользователя для очистки
    await safe_delete(message.bot, message.chat.id, message.message_id)

    lock = _user_draft_locks.get(message.from_user.id)
    if lock is None:
        lock = asyncio.Lock()
        _user_draft_locks[message.from_user.id] = lock
    async with lock:
        data = await state.get_data()
        media_type, file_id, text_content = extract_content(message)

        if media_type == "unknown":
            await notify(message, "⚠️ Пожалуйста, отправьте текст или фото.")
            return

        # Загружаем текущий черновик
        post_text = data.get("post_text")
        post_photos = list(data.get("post_photos", []))

        if media_type == "text" and text_content:
            post_text = text_content
        elif media_type == "photo" and file_id:
            post_photos.append(file_id)
            # Если есть описание к первому фото и текст еще не задан
            if text_content and not post_text:
                post_text = text_content

        await state.update_data(post_text=post_text, post_photos=post_photos)

        # Формируем статус черновика
        text_status = f"✅ (длина {len(post_text)})" if post_text else "❌ Не отправлен"
        photos_count = len(post_photos)

        draft_text = (
            "📝 <b>Создание новой публикации</b>\n\n"
            "Вы можете отправить текст и одну или несколько фотографий по очереди.\n\n"
            "<b>Текущий черновик:</b>\n"
            f"💬 Текст: {text_status}\n"
            f"🖼 Фотографий: <code>{photos_count}</code>\n\n"
            "Вы можете отправить новый текст (он перезапишет старый) или добавить ещё фотографии.\n"
            "Когда закончите, нажмите <b>✅ Готово</b>."
        )

        if data.get("prompt_msg_id"):
            try:
                await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=data["prompt_msg_id"],
                    text=draft_text,
                    reply_markup=draft_keyboard(),
                    parse_mode="HTML"
                )
            except Exception:
                await safe_delete(message.bot, message.chat.id, data["prompt_msg_id"])
                prompt = await message.answer(
                    draft_text,
                    reply_markup=draft_keyboard(),
                    parse_mode="HTML"
                )
                await state.update_data(prompt_msg_id=prompt.message_id)
        else:
            prompt = await message.answer(
                draft_text,
                reply_markup=draft_keyboard(),
                parse_mode="HTML"
            )
            await state.update_data(prompt_msg_id=prompt.message_id)


# ══════════════════════════════════════════════════════════
# FSM: Редактирование публикации
# ══════════════════════════════════════════════════════════

@router.message(EditPost.waiting_new_content)
async def edit_post_save(message: Message, state: FSMContext):
    data = await state.get_data()
    post_id = data.get("selected_post_id")

    media_type, file_id, text_content = extract_content(message)

    if media_type == "unknown":
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Отправьте текст, фото, видео или документ.")
        return

    if media_type == "text" and not text_content:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Текст не может быть пустым.")
        return

    if not post_id:
        await notify(message, "Ошибка: пост не выбран.")
        await state.set_state(None)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, "Пост не найден.")
            await state.set_state(None)
            return

        role = await get_user_role(session, message.from_user.id, post.channel_id)

        # Проверка лимита для user
        if role == "user":
            cu_result = await session.execute(
                select(ChannelUser).where(
                    ChannelUser.user_id == message.from_user.id,
                    ChannelUser.channel_id == post.channel_id,
                )
            )
            cu = cu_result.scalar_one_or_none()
            if cu:
                tz_name = await get_channel_timezone(session, post.channel_id)
                tz = ZoneInfo(tz_name)
                local_now = datetime.now(tz)
                local_today = local_now.date()

                if cu.edits_today_date:
                    last_edit_local = utc_to_local(cu.edits_today_date, tz_name)
                    if last_edit_local.date() < local_today:
                        cu.edits_today = 0

                if cu.edits_today >= DAILY_EDIT_LIMIT:
                    await delete_pair(message, data.get("prompt_msg_id"))
                    await notify(message, f"⛔️ Лимит: {DAILY_EDIT_LIMIT} редактирования в день.")
                    await state.set_state(None)
                    return

                cu.edits_today += 1
                cu.edits_today_date = utcnow()
                cu.last_edit_time = utcnow()
                await session.flush()

        if post.user_id != message.from_user.id and role not in ("moderator", "superadmin"):
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, "❌ У вас нет прав на редактирование.")
            await state.set_state(None)
            return

        # Получаем internal_id автора
        cu_result = await session.execute(
            select(ChannelUser.internal_id).where(
                ChannelUser.user_id == post.user_id,
                ChannelUser.channel_id == post.channel_id,
            )
        )
        author_id = cu_result.scalar_one_or_none() or "—"
        signature = f"Опубликовано: {author_id}"

        # Удаляем старый пост из канала
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

        # Отправляем новый
        try:
            sent = await send_to_channel(
                message.bot, post.channel_id,
                media_type, file_id, text_content or "", signature
            )
        except AiogramError as e:
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, f"❌ Не удалось обновить: {e}", delay=5)
            await state.set_state(None)
            return

        # Обновляем запись
        post.message_id = sent.message_id
        post.text = text_content or ""
        post.media_type = media_type
        post.file_ids = file_id
        post.last_edited_at = utcnow()
        await session.commit()

    await state.update_data(
        selected_post_id=None, prompt_msg_id=None
    )

    await notify(message, "✅ Публикация обновлена!", delay=3)
    await enter_posts_section(
        message.bot, message.chat.id,
        message.from_user.id, state
    )


# ══════════════════════════════════════════════════════════
# Система жалоб (Reports)
# ══════════════════════════════════════════════════════════

@router.message(F.text == "⚠️ Пожаловаться на пост")
async def report_post_start(message: Message, state: FSMContext):
    await safe_delete(message.bot, message.chat.id, message.message_id)

    channel_id = await get_active_channel(message, state)
    if not channel_id:
        await notify(message, "⚠️ Сначала подключите канал.", delay=4)
        return

    await state.set_state(ReportPost.waiting_post_reference)
    prompt = await message.answer(
        "⚠️ <b>Подача жалобы на публикацию</b>\n\n"
        "Пожалуйста, перешлите пост из канала, на который хотите пожаловаться:",
        reply_markup=back_only_keyboard(),
        parse_mode="HTML"
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


@router.message(ReportPost.waiting_post_reference)
async def report_post_reference(message: Message, state: FSMContext):
    data = await state.get_data()
    channel_id = data.get("active_channel_id")
    target_post = None

    async with AsyncSessionLocal() as session:
        # Если переслано из канала
        if message.forward_from_chat:
            if message.forward_from_chat.id == channel_id:
                result = await session.execute(
                    select(Post).where(
                        Post.channel_id == channel_id,
                        Post.message_id == message.forward_from_message_id,
                        Post.is_deleted == False
                    )
                )
                target_post = result.scalar_one_or_none()

        if not target_post:
            await safe_delete(message.bot, message.chat.id, message.message_id)
            await notify(
                message,
                "❌ Публикация не найдена.\nПожалуйста, перешлите именно пост из канала:",
                delay=4
            )
            return

        await state.update_data(report_target_post_id=target_post.id)
        tz_name = await get_channel_timezone(session, channel_id)
        local_dt = utc_to_local(target_post.created_at, tz_name)
        dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(ReportPost.waiting_reason)
    prompt = await message.answer(
        f"📝 Публикация от {dt_str} выбрана.\n"
        "Введите причину жалобы:",
        reply_markup=back_only_keyboard()
    )
    await state.update_data(prompt_msg_id=prompt.message_id)


@router.message(ReportPost.waiting_reason)
async def report_post_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    post_id = data.get("report_target_post_id")
    channel_id = data.get("active_channel_id")
    reason = message.text.strip() if message.text else ""

    if not reason:
        await safe_delete(message.bot, message.chat.id, message.message_id)
        await notify(message, "⚠️ Причина не может быть пустой.")
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Post).where(Post.id == post_id))
        post = result.scalar_one_or_none()
        if not post or post.is_deleted:
            await delete_pair(message, data.get("prompt_msg_id"))
            await notify(message, "Пост не найден.")
            await state.set_state(None)
            return

        cu_sender = await session.execute(
            select(ChannelUser.internal_id).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.user_id == message.from_user.id
            )
        )
        sender_id = cu_sender.scalar_one_or_none() or "—"

        channel_title = await get_channel_title(session, channel_id)

        admins_result = await session.execute(
            select(ChannelUser.user_id).where(
                ChannelUser.channel_id == channel_id,
                ChannelUser.role.in_(("moderator", "superadmin"))
            )
        )
        admin_ids = [row[0] for row in admins_result.all()]

        tz_name = await get_channel_timezone(session, channel_id)
        local_dt = utc_to_local(post.created_at, tz_name)
        dt_str = local_dt.strftime("%d.%m.%Y %H:%M") if local_dt else "—"

        report_text = (
            f"⚠️ <b>Жалоба в канале {channel_title}</b>\n\n"
            f"👤 Отправитель: <code>{sender_id}</code>\n"
            f"📄 Публикация от: <code>{dt_str}</code>\n"
            f"💬 Причина: <i>{reason}</i>"
        )

        # Записываем жалобы как уведомления для админов/модераторов
        ntfs_to_add = []
        for admin_id in admin_ids:
            ntfs_to_add.append(Notification(
                sender_id=message.from_user.id,
                receiver_id=admin_id,
                channel_id=channel_id,
                text=report_text,
                is_read=False,
                created_at=utcnow(),
                post_id=post.id
            ))
        session.add_all(ntfs_to_add)
        await session.commit()
        
        # Получаем их ID для алертов
        ntf_map = {n.receiver_id: n.id for n in ntfs_to_add}

    await delete_pair(message, data.get("prompt_msg_id"))
    await state.set_state(None)
    await state.update_data(prompt_msg_id=None, report_target_post_id=None)

    sent_count = 0
    for admin_id in admin_ids:
        try:
            nid = ntf_map.get(admin_id)
            if not nid:
                continue
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📖 Прочитать", callback_data=f"ntf_read_alert:{nid}")]
            ])
            await message.bot.send_message(
                chat_id=admin_id,
                text="🔔 <b>Поступила новая жалоба на публикацию!</b>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            sent_count += 1
        except Exception:
            pass

    if sent_count > 0:
        await notify(message, "✅ Жалоба отправлена администрации.", delay=4)
    else:
        await notify(message, "⚠️ Жалоба принята, но не удалось связаться с администрацией.", delay=4)

    # Возвращаемся в главное меню и восстанавливаем клавиатуру
    await show_main_menu(
        message.bot, message.chat.id, message.from_user.id, state
    )