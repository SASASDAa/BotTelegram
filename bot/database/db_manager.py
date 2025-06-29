# bot/database/db_manager.py
# Полностью переписано для работы с PostgreSQL с использованием asyncpg.
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from asyncpg.exceptions import DuplicateColumnError, UniqueViolationError

import config

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, dsn: str):
        """
        Инициализирует менеджер базы данных для PostgreSQL.
        :param dsn: Строка подключения к PostgreSQL (Data Source Name).
        """
        self.dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None
        self._pool_lock = asyncio.Lock()

    async def _get_pool(self) -> asyncpg.Pool:
        """Внутренний метод для получения пула соединений (или его создания)."""
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None: # Double-check locking
                    self._pool = await asyncpg.create_pool(self.dsn)
                    logger.info("Connection pool to PostgreSQL established.")
        return self._pool

    async def init_db(self):
        os.makedirs('sessions', exist_ok=True)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # --- Table Creation ---
                await conn.execute('''CREATE TABLE IF NOT EXISTS bot_users (
                                         user_id BIGINT PRIMARY KEY,
                                         username TEXT,
                                         subscription_until TIMESTAMP WITH TIME ZONE,
                                         is_banned BOOLEAN NOT NULL DEFAULT false,
                                         role TEXT NOT NULL DEFAULT 'user'
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS sessions (
                                         user_id BIGINT REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         phone TEXT,
                                         session_file TEXT,
                                         client_type TEXT NOT NULL DEFAULT 'pyrogram',
                                         PRIMARY KEY (user_id, phone, session_file)
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS delays (
                                         user_id BIGINT PRIMARY KEY REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         delay INTEGER
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS chats (
                                         user_id BIGINT REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         chat_identifier TEXT,
                                         PRIMARY KEY (user_id, chat_identifier)
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS comments (
                                         user_id BIGINT REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         comment_text TEXT,
                                         position INTEGER,
                                         PRIMARY KEY (user_id, position)
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS spam_media (
                                         user_id BIGINT PRIMARY KEY REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         photo_file_path TEXT
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS ai_settings (
                                         user_id BIGINT PRIMARY KEY REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         gemini_api_key TEXT,
                                         uniqueness_enabled BOOLEAN NOT NULL DEFAULT false,
                                         uniqueness_prompt TEXT,
                                         persistent_spam_enabled BOOLEAN NOT NULL DEFAULT false,
                                         use_proxy_enabled BOOLEAN NOT NULL DEFAULT true,
                                         auto_leave_enabled BOOLEAN NOT NULL DEFAULT false,
                                         attack_skip_admins BOOLEAN NOT NULL DEFAULT true,
                                         user_activity_filter TEXT NOT NULL DEFAULT 'all'
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS warmer_settings (
                                         user_id BIGINT PRIMARY KEY REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         duration_days INTEGER DEFAULT 7,
                                         join_channels_per_day INTEGER DEFAULT 2,
                                         send_reactions_per_day INTEGER DEFAULT 5,
                                         target_channels TEXT,
                                         dialogue_simulation_enabled BOOLEAN NOT NULL DEFAULT false,
                                         dialogue_phrases TEXT,
                                         dialogues_per_day INTEGER DEFAULT 3,
                                         active_hours_enabled BOOLEAN NOT NULL DEFAULT false,
                                         active_hours_start INTEGER DEFAULT 9,
                                         active_hours_end INTEGER DEFAULT 22,
                                         inform_user_on_action BOOLEAN NOT NULL DEFAULT false
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS scraped_users (
                                         user_id BIGINT REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         scraped_user_id BIGINT NOT NULL,
                                         username TEXT,
                                         source_group TEXT,
                                         scraped_at TIMESTAMP WITH TIME ZONE NOT NULL,
                                         PRIMARY KEY (user_id, scraped_user_id)
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS scheduled_tasks (
                                         job_id TEXT PRIMARY KEY,
                                         user_id BIGINT NOT NULL REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         task_type TEXT NOT NULL,
                                         task_params TEXT,
                                         cron_expression TEXT NOT NULL,
                                         created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                                         is_active BOOLEAN NOT NULL DEFAULT true,
                                         last_run_time TIMESTAMP WITH TIME ZONE
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
                                         key TEXT PRIMARY KEY,
                                         value TEXT
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS proxies (
                                         user_id BIGINT REFERENCES bot_users(user_id) ON DELETE CASCADE,
                                         proxy_string TEXT,
                                         PRIMARY KEY (user_id, proxy_string)
                                         )''')

                # --- Promo Code Table Migration and Creation ---
                await self._migrate_promo_codes(conn)

                # This handles fresh installs and ensures tables exist after a potential migration.
                await conn.execute('''CREATE TABLE IF NOT EXISTS promo_codes (
                                         code TEXT PRIMARY KEY,
                                         duration_days INTEGER NOT NULL,
                                         max_activations INTEGER NOT NULL,
                                         created_at TIMESTAMP WITH TIME ZONE NOT NULL
                                         )''')
                await conn.execute('''CREATE TABLE IF NOT EXISTS promo_code_activations (
                                         id SERIAL PRIMARY KEY,
                                         promo_code TEXT NOT NULL REFERENCES promo_codes(code) ON DELETE CASCADE,
                                         user_id BIGINT NOT NULL,
                                         activated_at TIMESTAMP WITH TIME ZONE NOT NULL
                                         )''')

                # --- Other Column Migrations ---
                # Helper to avoid repetitive try/except blocks
                async def _add_column(table, column, definition):
                    try:
                        await conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
                        logger.info(f"Column '{column}' added to '{table}' table.")
                    except DuplicateColumnError:
                        pass # Column already exists, which is fine

                # These migrations are for backwards compatibility.
                # New installations will have these columns from CREATE TABLE statements.
                await _add_column('bot_users', 'subscription_until', 'TIMESTAMP WITH TIME ZONE')
                await _add_column('bot_users', 'is_banned', 'BOOLEAN NOT NULL DEFAULT false')
                await _add_column('bot_users', 'role', "TEXT NOT NULL DEFAULT 'user'")
                await _add_column('ai_settings', 'persistent_spam_enabled', 'BOOLEAN NOT NULL DEFAULT false')
                await _add_column('ai_settings', 'use_proxy_enabled', 'BOOLEAN NOT NULL DEFAULT true')
                await _add_column('ai_settings', 'auto_leave_enabled', 'BOOLEAN NOT NULL DEFAULT false')
                await _add_column('ai_settings', 'attack_skip_admins', 'BOOLEAN NOT NULL DEFAULT true')
                await _add_column('ai_settings', 'user_activity_filter', "TEXT NOT NULL DEFAULT 'all'")
                await _add_column('sessions', 'client_type', "TEXT NOT NULL DEFAULT 'pyrogram'")
                await _add_column('warmer_settings', 'dialogue_simulation_enabled', 'BOOLEAN NOT NULL DEFAULT false')
                await _add_column('warmer_settings', 'dialogue_phrases', 'TEXT')
                await _add_column('warmer_settings', 'dialogues_per_day', 'INTEGER DEFAULT 3')
                await _add_column('warmer_settings', 'active_hours_enabled', 'BOOLEAN NOT NULL DEFAULT false')
                await _add_column('warmer_settings', 'active_hours_start', 'INTEGER DEFAULT 9')
                await _add_column('warmer_settings', 'active_hours_end', 'INTEGER DEFAULT 22')
                await _add_column('warmer_settings', 'inform_user_on_action', 'BOOLEAN NOT NULL DEFAULT false')

                # Migration for renaming 'keywords' to 'target_channels'
                has_keywords = await conn.fetchval("SELECT 1 FROM information_schema.columns WHERE table_name='warmer_settings' AND column_name='keywords'")
                has_target_channels = await conn.fetchval("SELECT 1 FROM information_schema.columns WHERE table_name='warmer_settings' AND column_name='target_channels'")
                if has_keywords and not has_target_channels:
                    await conn.execute("ALTER TABLE warmer_settings RENAME COLUMN keywords TO target_channels")
                    logger.info("Column 'keywords' in 'warmer_settings' renamed to 'target_channels'.")

    async def _migrate_promo_codes(self, conn: asyncpg.Connection):
        """Handles the migration of the old promo_codes table structure to the new one."""
        table_exists = await conn.fetchval("SELECT to_regclass('public.promo_codes')") is not None
        if not table_exists:
            return # Nothing to migrate

        # Check if it's the old version by looking for a column that was removed/changed.
        is_old_version = await conn.fetchval("SELECT 1 FROM information_schema.columns WHERE table_name='promo_codes' AND column_name='is_activated'") is not None
        if not is_old_version:
            return # Already new version or empty table

        logger.info("Old promo_codes table structure detected. Starting migration...")
        try:
            async with conn.transaction():
                await conn.execute('ALTER TABLE promo_codes RENAME TO promo_codes_old')
                await conn.execute('''CREATE TABLE promo_codes (
                                         code TEXT PRIMARY KEY,
                                         duration_days INTEGER NOT NULL,
                                         max_activations INTEGER NOT NULL,
                                         created_at TIMESTAMP WITH TIME ZONE NOT NULL
                                         )''')
                await conn.execute('''CREATE TABLE promo_code_activations (
                                         id SERIAL PRIMARY KEY,
                                         promo_code TEXT NOT NULL REFERENCES promo_codes (code) ON DELETE CASCADE,
                                         user_id BIGINT NOT NULL,
                                         activated_at TIMESTAMP WITH TIME ZONE NOT NULL
                                         )''')

                old_codes = await conn.fetch('SELECT code, duration_days, is_activated, activated_by_user_id, activated_at FROM promo_codes_old')
                for old_code in old_codes:
                    code, duration, is_activated, user_id, activated_at_dt = old_code
                    created_at = activated_at_dt if activated_at_dt else datetime.now(timezone.utc)
                    await conn.execute("INSERT INTO promo_codes (code, duration_days, max_activations, created_at) VALUES ($1, $2, $3, $4)",
                                       code, duration, 1, created_at)
                    if is_activated and user_id and activated_at_dt:
                        await conn.execute("INSERT INTO promo_code_activations (promo_code, user_id, activated_at) VALUES ($1, $2, $3)",
                                           code, user_id, activated_at_dt)
                await conn.execute('DROP TABLE promo_codes_old')
            logger.info("Promo code table migration completed successfully.")
        except Exception as e:
            logger.error(f"Promo code migration failed: {e}. Transaction rolled back.", exc_info=True)
            raise e

    async def close(self):
        """Закрывает пул соединений с базой данных."""
        pool = await self._get_pool()
        if self._pool:
            await pool.close()
            self._pool = None
            logger.info("PostgreSQL connection pool closed.")

    async def add_bot_user(self, user_id, username=None):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO bot_users (user_id, username) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING", user_id, username)

    async def update_delay(self, user_id, delay):
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO delays (user_id, delay) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET delay = EXCLUDED.delay
            """, user_id, delay)

    async def get_delay(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            delay = await conn.fetchval("SELECT delay FROM delays WHERE user_id=$1", user_id)
        return delay if delay is not None else config.DEFAULT_DELAY_BETWEEN_COMMENTS

    async def get_user_data(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            session_records = await conn.fetch("SELECT phone, session_file FROM sessions WHERE user_id=$1", user_id)
            sessions = {row['phone']: row['session_file'] for row in session_records}

            chat_records = await conn.fetch("SELECT chat_identifier FROM chats WHERE user_id=$1", user_id)
            chats = [row['chat_identifier'] for row in chat_records]

            proxy_records = await conn.fetch("SELECT proxy_string FROM proxies WHERE user_id=$1", user_id)
            proxies = [row['proxy_string'] for row in proxy_records]

        return {'sessions': sessions, 'chats': chats, 'proxies': proxies}

    async def delete_session_by_filepath(self, user_id, session_file_path):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM sessions WHERE user_id=$1 AND session_file=$2", user_id, session_file_path)

    async def add_session(self, user_id, phone, session_file_path, client_type: str = 'pyrogram'):
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # Using ON CONFLICT to handle potential replacements cleanly
            await conn.execute("""
                INSERT INTO sessions (user_id, phone, session_file, client_type) VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, phone, session_file) DO UPDATE SET client_type = EXCLUDED.client_type
            """, user_id, phone, session_file_path, client_type)
        logger.info(f"Session for {phone} added for {user_id} at {session_file_path}")

    async def delete_session(self, user_id, phone):
        session_file_to_delete = None
        pool = await self._get_pool()
        phone_plus = '+' + phone if not phone.startswith('+') else phone

        async with pool.acquire() as conn:
            # The OR is for legacy compatibility in case a phone was stored without '+'
            record = await conn.fetchrow("SELECT session_file FROM sessions WHERE user_id=$1 AND (phone=$2 OR phone=$3)", user_id, phone, phone_plus)
            if record:
                session_file_to_delete = record['session_file']

            if not session_file_to_delete:
                logger.warning(f"Attempted to delete session for phone {phone} (user {user_id}), but no DB entry was found.")
                return

            # Delete from DB
            await conn.execute("DELETE FROM sessions WHERE user_id=$1 AND (phone=$2 OR phone=$3)", user_id, phone, phone_plus)
            logger.info(f"Session for {phone} of user {user_id} deleted from DB.")

        # Delete from filesystem
        if os.path.exists(session_file_to_delete):
            try:
                # Get the base name to delete all related files (.session, .session-journal, etc.)
                session_dir = os.path.dirname(session_file_to_delete)
                session_name_base = os.path.splitext(os.path.basename(session_file_to_delete))[0]

                for filename in os.listdir(session_dir):
                    if filename.startswith(session_name_base) and os.path.isfile(os.path.join(session_dir, filename)):
                        os.remove(os.path.join(session_dir, filename))
                        logger.info(f"Removed session-related file: {os.path.join(session_dir, filename)}")
            except Exception as e:
                logger.error(f"Error during filesystem cleanup for session {session_file_to_delete}: {e}")

    async def add_chats(self, user_id, chat_identifiers_list):
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        valid_chats = []
        for chat_id in chat_identifiers_list:
            clean_chat_id = chat_id.strip()
            if clean_chat_id:
                valid_chats.append((user_id, clean_chat_id))

        if valid_chats:
            async with pool.acquire() as conn:
                await conn.executemany("INSERT INTO chats (user_id, chat_identifier) VALUES ($1, $2) ON CONFLICT DO NOTHING", valid_chats)
            logger.info(f"{len(valid_chats)} chats added/updated for {user_id}")

    async def delete_chat(self, user_id, chat_identifier):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chats WHERE user_id=$1 AND chat_identifier=$2", user_id, chat_identifier)
        logger.info(f"Chat {chat_identifier} deleted for {user_id}")

    async def update_comments(self, user_id, comment_texts):
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM comments WHERE user_id=$1", (user_id,))
                data_to_insert = [(user_id, text.strip(), idx) for idx, text in enumerate(comment_texts)]
                if data_to_insert:
                    await conn.executemany("INSERT INTO comments (user_id, comment_text, position) VALUES ($1, $2, $3)", data_to_insert)
        logger.info(f"Comments updated for user {user_id}. Count: {len(comment_texts)}")

    async def get_comments(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT comment_text FROM comments WHERE user_id=$1 ORDER BY position", user_id)
            return [row['comment_text'] for row in records]

    async def get_chats_count(self, user_id: int) -> int:
        """Возвращает количество чатов пользователя, не загружая их."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM chats WHERE user_id=$1", user_id)
        return count or 0

    async def get_paginated_chats(self, user_id: int, page: int, page_size: int) -> list[str]:
        """Возвращает определенную страницу из списка чатов пользователя."""
        pool = await self._get_pool()
        offset = (page - 1) * page_size
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT chat_identifier FROM chats WHERE user_id=$1 ORDER BY ctid LIMIT $2 OFFSET $3", user_id, page_size, offset)
            return [row['chat_identifier'] for row in records]

    async def get_chats_stream(self, user_id: int):
        """Асинхронно отдает идентификаторы чатов по одному для экономии памяти."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(): # Cursors must be used in a transaction
                async for record in conn.cursor("SELECT chat_identifier FROM chats WHERE user_id=$1", user_id):
                    yield record['chat_identifier']

    async def reset_sessions(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # First, get all session file paths for the user
            records = await conn.fetch("SELECT session_file FROM sessions WHERE user_id=$1", user_id)
            session_files_to_delete = [row['session_file'] for row in records]

            # Then, delete from DB
            await conn.execute("DELETE FROM sessions WHERE user_id=$1", user_id)
            logger.info(f"All sessions for {user_id} have been deleted from DB.")

        # Finally, delete files from filesystem
        for session_file_path in session_files_to_delete:
            if os.path.exists(session_file_path):
                try:
                    session_dir = os.path.dirname(session_file_path)
                    session_name_base = os.path.splitext(os.path.basename(session_file_path))[0]
                    for filename in os.listdir(session_dir):
                        if filename.startswith(session_name_base) and os.path.isfile(os.path.join(session_dir, filename)):
                            os.remove(os.path.join(session_dir, filename))
                            logger.info(f"Removed session-related file during reset: {os.path.join(session_dir, filename)}")
                except Exception as e:
                    logger.error(f"Error during filesystem cleanup for session {session_file_path} during reset: {e}")

    async def reset_chats(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM chats WHERE user_id=$1", user_id)
        logger.info(f"All chats for {user_id} have been deleted.")

    async def reset_comments(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM comments WHERE user_id=$1", user_id)
        await self.delete_spam_photo(user_id)
        logger.info(f"All comments and spam media for user {user_id} have been deleted.")

    async def get_sessions_with_details(self, user_id: int) -> list[dict]:
        """Возвращает список словарей с деталями сессий пользователя."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT phone, session_file, client_type FROM sessions WHERE user_id=$1", user_id)
            return [{'phone': r['phone'], 'session_file': r['session_file'], 'client_type': r['client_type']} for r in records]

    async def get_sessions_by_type(self, user_id: int, client_type: str) -> dict[str, str]:
        """Возвращает сессии определенного типа в формате {phone: path}."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT phone, session_file FROM sessions WHERE user_id=$1 AND client_type=$2", user_id, client_type)
            return {row['phone']: row['session_file'] for row in records}

    async def get_session_counts(self, user_id: int) -> dict[str, int]:
        """Возвращает количество сессий каждого типа."""
        pool = await self._get_pool()
        counts = {'pyrogram': 0, 'telethon': 0}
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT client_type, COUNT(*) as count FROM sessions WHERE user_id=$1 GROUP BY client_type", user_id)
            for record in records:
                if record['client_type'] in counts:
                    counts[record['client_type']] = record['count']
        return counts

    async def reset_scraped_users(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM scraped_users WHERE user_id=$1", user_id)
        logger.info(f"All scraped users for {user_id} have been deleted.")

    async def add_proxy(self, user_id, proxy_string):
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO proxies (user_id, proxy_string) VALUES ($1, $2) ON CONFLICT DO NOTHING", user_id, proxy_string)
        logger.info(f"Proxy {proxy_string} added for {user_id}")

    async def delete_proxy(self, user_id, proxy_string):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM proxies WHERE user_id=$1 AND proxy_string=$2", user_id, proxy_string)
        logger.info(f"Proxy {proxy_string} deleted for {user_id}")

    async def get_proxies(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT proxy_string FROM proxies WHERE user_id=$1", user_id)
            return [row['proxy_string'] for row in records]

    async def reset_proxies(self, user_id):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM proxies WHERE user_id=$1", user_id)
        logger.info(f"All proxies for {user_id} have been deleted.")

    # --- AI Settings Methods ---
    async def _upsert_ai_setting(self, user_id: int, **kwargs):
        """Helper to update one or more AI settings."""
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        if not kwargs: return

        columns = ", ".join(kwargs.keys())
        values = ", ".join(f"${i+2}" for i in range(len(kwargs)))
        updates = ", ".join(f"{key} = EXCLUDED.{key}" for key in kwargs.keys())

        query = f"""
            INSERT INTO ai_settings (user_id, {columns}) VALUES ($1, {values})
            ON CONFLICT (user_id) DO UPDATE SET {updates}
        """
        async with pool.acquire() as conn:
            await conn.execute(query, user_id, *kwargs.values())

    async def set_gemini_api_key(self, user_id, api_key):
        await self._upsert_ai_setting(user_id, gemini_api_key=api_key)
        logger.info(f"Gemini API key set for user {user_id}")

    async def set_uniqueness_prompt(self, user_id, prompt):
        await self._upsert_ai_setting(user_id, uniqueness_prompt=prompt)
        logger.info(f"Uniqueness prompt set for user {user_id}")

    async def set_uniqueness_enabled(self, user_id, enabled: bool):
        await self._upsert_ai_setting(user_id, uniqueness_enabled=enabled)
        logger.info(f"Gemini uniqueness {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_proxy_enabled(self, user_id, enabled: bool):
        await self._upsert_ai_setting(user_id, use_proxy_enabled=enabled)
        logger.info(f"Proxy usage {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_persistent_spam_enabled(self, user_id, enabled: bool):
        await self._upsert_ai_setting(user_id, persistent_spam_enabled=enabled)
        logger.info(f"Persistent spam {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_auto_leave_enabled(self, user_id, enabled: bool):
        await self._upsert_ai_setting(user_id, auto_leave_enabled=enabled)
        logger.info(f"Auto-leave groups {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_attack_skip_admins(self, user_id, enabled: bool):
        await self._upsert_ai_setting(user_id, attack_skip_admins=enabled)
        logger.info(f"Attack skip admins {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_user_activity_filter(self, user_id, filter_level: str):
        await self._upsert_ai_setting(user_id, user_activity_filter=filter_level)
        logger.info(f"User activity filter set to '{filter_level}' for {user_id}")

    async def get_ai_settings(self, user_id: int) -> dict:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchrow("SELECT * FROM ai_settings WHERE user_id=$1", user_id)
        if result:
            return {
                "api_key": result['gemini_api_key'],
                "enabled": result['uniqueness_enabled'],
                "prompt": result['uniqueness_prompt'] or config.DEFAULT_GEMINI_PROMPT,
                "persistent_spam": result['persistent_spam_enabled'],
                "use_proxy": result['use_proxy_enabled'],
                "auto_leave_enabled": result['auto_leave_enabled'],
                "attack_skip_admins": result['attack_skip_admins'],
                "user_activity_filter": result['user_activity_filter'] or 'all'
            }
        return {"api_key": None, "enabled": False, "prompt": config.DEFAULT_GEMINI_PROMPT, "persistent_spam": False, "use_proxy": True, "auto_leave_enabled": False, "attack_skip_admins": True, "user_activity_filter": "all"}

    # --- Scraped Users Methods ---
    async def add_scraped_users(self, user_id: int, source_group: str, users: list[dict]):
        """Adds a list of scraped users to the database."""
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        data_to_insert = [
            (user_id, user['id'], user.get('username'), source_group, now)
            for user in users
        ]
        if not data_to_insert:
            return

        async with pool.acquire() as conn:
            await conn.executemany("""
                INSERT INTO scraped_users (user_id, scraped_user_id, username, source_group, scraped_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, scraped_user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    source_group = EXCLUDED.source_group,
                    scraped_at = EXCLUDED.scraped_at
            """, data_to_insert)
        logger.info(f"Added/updated {len(data_to_insert)} scraped users for user {user_id} from {source_group}.")

    async def get_scraped_users_count(self, user_id: int) -> int:
        """Returns the count of scraped users for a user."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM scraped_users WHERE user_id=$1", user_id)
        return count or 0

    async def get_scraped_users_stream(self, user_id: int):
        """Asynchronously yields scraped user IDs."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                async for record in conn.cursor("SELECT scraped_user_id FROM scraped_users WHERE user_id=$1", user_id):
                    yield record['scraped_user_id']

    async def import_scraped_users(self, user_id: int, user_ids: list[int]) -> int:
        """Imports a list of user IDs into the scraped users table."""
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        source_group = "imported"  # A special source for imported users

        if not user_ids:
            return 0

        data_to_insert = [
            (user_id, uid, None, source_group, now)
            for uid in set(user_ids) # Use set to remove duplicates from input list
        ]

        async with pool.acquire() as conn:
            # ON CONFLICT DO NOTHING handles duplicates gracefully.
            # The result status gives the number of rows affected.
            result = await conn.executemany(
                "INSERT INTO scraped_users (user_id, scraped_user_id, username, source_group, scraped_at) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                data_to_insert
            )
        # 'result' is a list of statuses, e.g., [('INSERT 0 1',), ...]. We count how many were successful inserts.
        inserted_count = sum(1 for status in result if status.startswith('INSERT'))
        logger.info(f"Imported {inserted_count} new scraped users for user {user_id}.")
        return inserted_count

    # --- Warmer Settings Methods ---
    async def get_warmer_settings(self, user_id: int) -> dict:
        """Gets warmer settings for a user, returning defaults if not set."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchrow("SELECT * FROM warmer_settings WHERE user_id=$1", user_id)
        if result:
            return {
                "duration_days": result['duration_days'],
                "join_channels_per_day": result['join_channels_per_day'],
                "send_reactions_per_day": result['send_reactions_per_day'],
                "target_channels": result['target_channels'] or "",
                "inform_user_on_action": result['inform_user_on_action'],
                "dialogue_simulation_enabled": result['dialogue_simulation_enabled'],
                "dialogue_phrases": result['dialogue_phrases'] or "Привет,Как дела?,Что нового?",
                "dialogues_per_day": result['dialogues_per_day'],
                "active_hours_enabled": result['active_hours_enabled'],
                "active_hours_start": result['active_hours_start'],
                "active_hours_end": result['active_hours_end'],
            }
        return {
            "duration_days": 7,
            "join_channels_per_day": 2,
            "send_reactions_per_day": 5,
            "target_channels": "",
            "inform_user_on_action": False,
            "dialogue_simulation_enabled": False,
            "dialogue_phrases": "Привет,Как дела?,Что нового?",
            "dialogues_per_day": 3,
            "active_hours_enabled": False,
            "active_hours_start": 9,
            "active_hours_end": 22,
        }

    async def update_warmer_settings(self, user_id: int, settings: dict):
        """Updates one or more warmer settings for a user."""
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        if not settings: return

        columns = ", ".join(settings.keys())
        values = ", ".join(f"${i+2}" for i in range(len(settings)))
        updates = ", ".join(f"{key} = EXCLUDED.{key}" for key in settings.keys())

        query = f"""
            INSERT INTO warmer_settings (user_id, {columns}) VALUES ($1, {values})
            ON CONFLICT (user_id) DO UPDATE SET {updates}
        """
        async with pool.acquire() as conn:
            await conn.execute(query, user_id, *settings.values())

        logger.info(f"Warmer settings updated for user {user_id} with {settings}")

    # --- Scheduler Methods ---
    async def add_scheduled_task(self, job_id: str, user_id: int, task_type: str, cron_expression: str, task_params: str):
        """Adds a new scheduled task to the database."""
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO scheduled_tasks (job_id, user_id, task_type, task_params, cron_expression, created_at) VALUES ($1, $2, $3, $4, $5, $6)",
                job_id, user_id, task_type, task_params, cron_expression, now
            )
        logger.info(f"Scheduled task {job_id} of type {task_type} added for user {user_id}.")

    async def get_active_scheduled_tasks(self) -> list[dict]:
        """Gets all active scheduled tasks to load into the scheduler on startup."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT job_id, user_id, task_type, task_params, cron_expression FROM scheduled_tasks WHERE is_active = true")
        return [{"job_id": r['job_id'], "user_id": r['user_id'], "task_type": r['task_type'], "task_params": r['task_params'], "cron": r['cron_expression']} for r in records]

    async def get_scheduled_tasks_for_user(self, user_id: int) -> list[dict]:
        """Gets all scheduled tasks for a specific user to display in the menu."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT job_id, task_type, cron_expression FROM scheduled_tasks WHERE user_id = $1 ORDER BY created_at DESC", user_id)
        return [{"job_id": r['job_id'], "task_type": r['task_type'], "cron": r['cron_expression']} for r in records]

    async def remove_scheduled_task(self, job_id: str):
        """Removes a scheduled task from the database by its ID."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM scheduled_tasks WHERE job_id = $1", job_id)
        logger.info(f"Scheduled task {job_id} removed from the database.")

    # --- Spam Media Methods ---
    async def set_spam_photo(self, user_id: int, photo_file_path: str):
        """Saves or updates the file_path of the photo for spamming."""
        await self.add_bot_user(user_id)
        old_path = await self.get_spam_photo(user_id)
        if old_path and os.path.exists(old_path) and old_path != photo_file_path:
            try:
                os.remove(old_path)
                logger.info(f"Old spam photo at {old_path} deleted for user {user_id}.")
            except OSError as e:
                logger.error(f"Error deleting old spam photo {old_path}: {e}")

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO spam_media (user_id, photo_file_path) VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET photo_file_path = EXCLUDED.photo_file_path
            """, user_id, photo_file_path)
        logger.info(f"Spam photo path set for user {user_id}")

    async def get_spam_photo(self, user_id: int) -> Optional[str]:
        """Gets the file_path of the photo for spamming."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            path = await conn.fetchval("SELECT photo_file_path FROM spam_media WHERE user_id=$1", user_id)
        return path

    async def delete_spam_photo(self, user_id: int):
        """Deletes the photo for spamming from DB and filesystem."""
        photo_path = await self.get_spam_photo(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM spam_media WHERE user_id=$1", user_id)

        if photo_path and os.path.exists(photo_path):
            try:
                os.remove(photo_path)
                logger.info(f"Spam photo file {photo_path} deleted for user {user_id}.")
            except OSError as e:
                logger.error(f"Error deleting spam photo file {photo_path}: {e}")

    # --- Subscription and Admin Methods ---
    async def set_ban_status(self, user_id: int, ban_status: bool):
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE bot_users SET is_banned = $1 WHERE user_id = $2", ban_status, user_id)
        logger.info(f"User {user_id} ban status set to {ban_status}")

    async def grant_subscription(self, user_id: int, days: int) -> datetime:
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        status = await self.get_subscription_status(user_id)
        current_expiry = status['expires_at']
        
        now = datetime.now(timezone.utc)
        start_date = current_expiry if current_expiry and current_expiry > now else now
        new_expiry_date = start_date + timedelta(days=days)
        
        async with pool.acquire() as conn:
            await conn.execute("UPDATE bot_users SET subscription_until = $1 WHERE user_id = $2", new_expiry_date, user_id)
        logger.info(f"Subscription for user {user_id} extended by {days} days. New expiry: {new_expiry_date}")
        return new_expiry_date

    async def get_subscription_status(self, user_id: int) -> dict:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchrow("SELECT subscription_until, is_banned FROM bot_users WHERE user_id=$1", user_id)
        
        if not result:
            return {"active": False, "expires_at": None, "is_banned": False}

        expires_at = result['subscription_until']
        is_banned = result['is_banned']
        is_active = expires_at > datetime.now(timezone.utc) if expires_at else False

        return {"active": is_active, "expires_at": expires_at, "is_banned": is_banned}

    async def get_user_role(self, user_id: int) -> str:
        if user_id == config.SUPER_ADMIN_ID:
            return 'super_admin'
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            role = await conn.fetchval("SELECT role FROM bot_users WHERE user_id=$1", user_id)
        # Возвращаем 'user' по умолчанию, если не найден или роль NULL
        return role or 'user'

    async def set_user_role(self, user_id: int, role: str):
        if role not in ['user', 'admin']:
            logger.warning(f"Попытка установить неверную роль '{role}' для пользователя {user_id}. Отмена.")
            return
        # Роль суперадмина неявная и не может быть установлена в БД
        if user_id == config.SUPER_ADMIN_ID:
            logger.warning(f"Попытка изменить роль для SUPER_ADMIN_ID {user_id}. Отмена.")
            return
        await self.add_bot_user(user_id)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE bot_users SET role = $1 WHERE user_id = $2", role, user_id)
        logger.info(f"Роль пользователя {user_id} установлена на '{role}'")

    async def get_all_admins(self) -> list[dict]:
        pool = await self._get_pool()
        # Сначала добавляем суперадмина
        admins = [{'user_id': config.SUPER_ADMIN_ID, 'role': 'super_admin', 'username': 'N/A'}]
        async with pool.acquire() as conn:
            db_admins = await conn.fetch("SELECT user_id, username FROM bot_users WHERE role='admin'")
            for admin in db_admins:
                admins.append({'user_id': admin['user_id'], 'role': 'admin', 'username': admin['username']})
        return admins

    async def get_all_user_ids(self) -> list[int]:
        """Returns a list of all non-banned user IDs from the bot_users table."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT user_id FROM bot_users WHERE is_banned = false")
        return [row['user_id'] for row in records]

    async def get_bot_stats(self) -> dict:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM bot_users")
            
            now = datetime.now(timezone.utc)
            active_subscriptions = await conn.fetchval("SELECT COUNT(*) FROM bot_users WHERE subscription_until > $1", now)
            
        return {"total_users": total_users or 0, "active_subscriptions": active_subscriptions or 0}

    # --- Promo Code Methods ---
    async def create_promo_code(self, code: str, days: int, max_activations: int):
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO promo_codes (code, duration_days, max_activations, created_at) VALUES ($1, $2, $3, $4)",
                code, days, max_activations, now
            )
        logger.info(f"Promo code '{code}' for {days} days created with {max_activations} max activations.")

    async def get_promo_code_details(self, code: str) -> dict | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            promo_result = await conn.fetchrow("SELECT duration_days, max_activations FROM promo_codes WHERE code=$1", code)
            if not promo_result:
                return None

            activations = await conn.fetch("SELECT user_id, activated_at FROM promo_code_activations WHERE promo_code=$1", code)

        return {
            "code": code,
            "duration_days": promo_result['duration_days'],
            "max_activations": promo_result['max_activations'],
            "current_activations": len(activations),
            "activations": [{"user_id": r['user_id'], "activated_at": r['activated_at']} for r in activations]
        }

    async def has_user_activated_code(self, code: str, user_id: int) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1 FROM promo_code_activations WHERE promo_code=$1 AND user_id=$2", code, user_id)
            return result is not None

    async def activate_promo_code(self, code: str, user_id: int):
        pool = await self._get_pool()
        now = datetime.now(timezone.utc)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO promo_code_activations (promo_code, user_id, activated_at) VALUES ($1, $2, $3)",
                code, user_id, now
            )
        logger.info(f"Promo code '{code}' activated by user {user_id}.")

    async def get_all_promo_codes_details(self) -> list[dict]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT p.code, p.duration_days, p.max_activations, COUNT(a.id) as current_activations FROM promo_codes p LEFT JOIN promo_code_activations a ON p.code = a.promo_code GROUP BY p.code ORDER BY p.created_at DESC")
            return [dict(r) for r in records]

    async def delete_promo_code(self, code: str):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM promo_codes WHERE code=$1", (code,))
        logger.info(f"Promo code '{code}' deleted.")

    # --- Bot Settings ---
    async def get_bot_setting(self, key: str) -> Optional[str]:
        """Gets a global bot setting from the database."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval("SELECT value FROM bot_settings WHERE key=$1", key)
        return value

    async def set_bot_setting(self, key: str, value: str):
        """Sets a global bot setting in the database."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO bot_settings (key, value) VALUES ($1, $2)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, key, value)

# Создаем один экземпляр менеджера для использования во всем приложении
# Убедитесь, что в config.py есть POSTGRES_DSN
db_manager = DatabaseManager(config.POSTGRES_DSN)