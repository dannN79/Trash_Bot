from aiogram.fsm.state import State, StatesGroup


class NewPost(StatesGroup):
    waiting_content = State()  # текст / фото / видео / документ


class EditPost(StatesGroup):
    waiting_new_content = State()


class DeletePost(StatesGroup):
    waiting_post_number = State()


class ReportPost(StatesGroup):
    waiting_post_reference = State()
    waiting_reason = State()


class AdminStates(StatesGroup):
    waiting_moderator_internal_id = State()
    waiting_ban_user_internal_id = State()
    waiting_ban_duration = State()
    waiting_ban_reason = State()
    waiting_custom_timezone = State()
    waiting_post_delete_reason = State()
    waiting_notify_all_text = State()
    waiting_notify_one_target = State()
    waiting_notify_one_text = State()


class NotificationStates(StatesGroup):
    waiting_recipient_type = State()
    waiting_target_user = State()
    waiting_text = State()


class ChangeID(StatesGroup):
    waiting_new_id = State()