# bot/states.py
from aiogram.fsm.state import State, StatesGroup

class SessionStates(StatesGroup):
    adding_phone = State()
    adding_code = State()
    adding_password = State()
    uploading_session = State()

class ChatStates(StatesGroup):
    add_list = State()

class SpamStates(StatesGroup):
    select_sessions = State()
    set_session_count = State()

class ProfileStates(StatesGroup):
    enter_promo_code = State()

class CommentStates(StatesGroup):
    enter_text = State()
    add_photo = State()

class DelayStates(StatesGroup):
    enter_delay = State()

class ProxyStates(StatesGroup):
    add_proxy = State()

class SearchStates(StatesGroup):
    enter_keywords = State()

class ScraperStates(StatesGroup):
    enter_group = State()
    view_scraped = State()

class AiStates(StatesGroup):
    set_key = State()
    set_prompt = State()

class AttackStates(StatesGroup):
    select_sessions = State()
    set_session_count = State()
    menu = State()
    set_nickname = State()
    set_count = State()
    set_delay = State()

class WarmerStates(StatesGroup):
    menu = State()
    set_duration = State()
    set_joins = State()
    set_reactions = State()
    set_target_channels = State()
    set_dialogues = State()
    set_dialogue_phrases = State()
    set_active_hours = State()

class SchedulerStates(StatesGroup):
    choose_task_type = State()
    enter_cron = State()
    enter_spam_params = State()
    enter_attack_mode = State()
    enter_attack_target = State()
    enter_attack_count = State()
    enter_attack_delay = State()
    enter_attack_sessions = State()

class AdminStates(StatesGroup):
    broadcast_message = State()
    broadcast_confirm = State()
    grant_sub_user_id = State()
    grant_sub_days = State()
    user_info_id = State()
    ban_user_id = State()
    ban_user_confirm = State()
    create_promo_code_days = State()
    create_promo_code_type = State()
    create_promo_code_activations = State()
    add_admin_id = State()
    restart_confirm = State()
    set_support_contact = State()