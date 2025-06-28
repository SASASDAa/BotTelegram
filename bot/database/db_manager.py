# bot/database/db_manager.py
import asyncio  # Added for asyncio.Lock
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
import config

logger = logging.getLogger(__name__)

class DatabaseManager:
    def __init__(self, db_name):
        self.db_name = db_name
        self._pool = None # To hold the single shared connection
        self._pool_lock = asyncio.Lock() # To ensure pool is initialized once

    async def _get_connection(self):
        """Internal method to get a connection from the pool (or create it if not exists)."""
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None: # Double-check locking
                    self._pool = await aiosqlite.connect(self.db_name)
                    await self._pool.execute("PRAGMA journal_mode=WAL;")
                    logger.info(f"Database connection established for {self.db_name}")
        return self._pool

    async def init_db(self):
        os.makedirs('sessions', exist_ok=True)
        conn = await self._get_connection() # Use the shared connection
            
        await conn.execute('''CREATE TABLE IF NOT EXISTS bot_users
                         (user_id INTEGER PRIMARY KEY, username TEXT, subscription_until DATETIME)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS sessions
                         (user_id INTEGER, phone TEXT, session_file TEXT,
                          PRIMARY KEY (user_id, phone),
                          FOREIGN KEY (user_id) REFERENCES bot_users(user_id) ON DELETE CASCADE)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS delays
                         (user_id INTEGER PRIMARY KEY, delay INTEGER,
                          FOREIGN KEY (user_id) REFERENCES bot_users(user_id))''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS chats (
                         user_id INTEGER, chat_identifier TEXT,
                         PRIMARY KEY (user_id, chat_identifier),
                         FOREIGN KEY (user_id) REFERENCES bot_users(user_id))''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS comments
                         (user_id INTEGER, comment_text TEXT, position INTEGER,
                          PRIMARY KEY (user_id, position),
                          FOREIGN KEY (user_id) REFERENCES bot_users(user_id))''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS spam_media (
                         user_id INTEGER PRIMARY KEY,
                         photo_file_path TEXT,
                         FOREIGN KEY (user_id) REFERENCES bot_users(user_id) ON DELETE CASCADE
                         )''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS ai_settings (
                         user_id INTEGER PRIMARY KEY,
                         gemini_api_key TEXT,
                         uniqueness_enabled INTEGER DEFAULT 0,
                         uniqueness_prompt TEXT,
                         FOREIGN KEY (user_id) REFERENCES bot_users(user_id)
                         )''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS warmer_settings (
                                 user_id INTEGER PRIMARY KEY,
                                 duration_days INTEGER DEFAULT 7,
                                 join_channels_per_day INTEGER DEFAULT 2,
                                 send_reactions_per_day INTEGER DEFAULT 5,
                                 keywords TEXT,
                                 FOREIGN KEY (user_id) REFERENCES bot_users(user_id) ON DELETE CASCADE
                                 )''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS scraped_users (
                         user_id INTEGER,
                         scraped_user_id INTEGER NOT NULL,
                         username TEXT,
                         source_group TEXT,
                         scraped_at DATETIME NOT NULL,
                         PRIMARY KEY (user_id, scraped_user_id),
                         FOREIGN KEY (user_id) REFERENCES bot_users(user_id) ON DELETE CASCADE
                         )''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS scheduled_tasks (
                                 job_id TEXT PRIMARY KEY,
                                 user_id INTEGER NOT NULL,
                                 task_type TEXT NOT NULL,
                                 task_params TEXT,
                                 cron_expression TEXT NOT NULL,
                                 created_at DATETIME NOT NULL,
                                 is_active INTEGER DEFAULT 1,
                                 last_run_time DATETIME,
                                 FOREIGN KEY (user_id) REFERENCES bot_users(user_id) ON DELETE CASCADE
                                 )''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS bot_settings (
                                 key TEXT PRIMARY KEY,
                                 value TEXT
                                 )''')

        # --- Promo Code Table Migration and Creation ---
        cursor = await conn.cursor()
        needs_migration = False
        # First, check if the promo_codes table exists at all.
        await cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='promo_codes'")
        table_exists = await cursor.fetchone()

        if table_exists:
            # If the table exists, check if it's the old version (lacks the new column).
            try:
                await cursor.execute("SELECT max_activations FROM promo_codes LIMIT 1")
                # If this succeeds, it's the new version, no migration needed.
            except aiosqlite.OperationalError as e:
                if "no such column" in str(e):
                    # This is the old version, migration is needed.
                    needs_migration = True
                else:
                    # Some other unexpected DB error.
                    await cursor.close()
                    raise e
        await cursor.close()

        if needs_migration:
            logger.info("Old promo_codes table structure detected. Starting migration...")
            try:
                await conn.execute('BEGIN')
                await conn.execute('ALTER TABLE promo_codes RENAME TO promo_codes_old')
                await conn.execute('''CREATE TABLE promo_codes (
                                         code TEXT PRIMARY KEY,
                                         duration_days INTEGER NOT NULL,
                                         max_activations INTEGER NOT NULL,
                                         created_at DATETIME NOT NULL
                                         )''')
                await conn.execute('''CREATE TABLE promo_code_activations (
                                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                                         promo_code TEXT NOT NULL,
                                         user_id INTEGER NOT NULL,
                                         activated_at DATETIME NOT NULL,
                                         FOREIGN KEY (promo_code) REFERENCES promo_codes (code) ON DELETE CASCADE
                                         )''')
                async with conn.cursor() as c_old:
                    await c_old.execute('SELECT code, duration_days, is_activated, activated_by_user_id, activated_at FROM promo_codes_old')
                    old_codes = await c_old.fetchall()
                    for old_code in old_codes:
                        code, duration, is_activated, user_id, activated_at_str = old_code
                        created_at = datetime.fromisoformat(activated_at_str) if activated_at_str else datetime.now()
                        await conn.execute("INSERT INTO promo_codes (code, duration_days, max_activations, created_at) VALUES (?, ?, ?, ?)",
                                           (code, duration, 1, created_at))
                        if is_activated and user_id and activated_at_str:
                            await conn.execute("INSERT INTO promo_code_activations (promo_code, user_id, activated_at) VALUES (?, ?, ?)",
                                               (code, user_id, datetime.fromisoformat(activated_at_str)))
                await conn.execute('DROP TABLE promo_codes_old')
                await conn.commit()
                logger.info("Promo code table migration completed successfully.")
            except Exception as e:
                logger.error(f"Promo code migration failed: {e}. Rolling back.", exc_info=True)
                await conn.rollback()
                raise e

        # This handles fresh installs and ensures tables exist after a potential migration.
        await conn.execute('''CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, duration_days INTEGER NOT NULL, max_activations INTEGER NOT NULL, created_at DATETIME NOT NULL)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS promo_code_activations (id INTEGER PRIMARY KEY AUTOINCREMENT, promo_code TEXT NOT NULL, user_id INTEGER NOT NULL, activated_at DATETIME NOT NULL, FOREIGN KEY (promo_code) REFERENCES promo_codes (code) ON DELETE CASCADE)''')
        await conn.commit()

        # --- Other Migrations ---
        try:
            await conn.execute("ALTER TABLE bot_users ADD COLUMN subscription_until DATETIME")
            await conn.commit()
            logger.info("Column 'subscription_until' added to 'bot_users' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass  # Column already exists, which is fine
            else:
                raise e
        # Add persistent_spam_enabled column to ai_settings if it doesn't exist (for migration)
        try:
            await conn.execute("ALTER TABLE ai_settings ADD COLUMN persistent_spam_enabled INTEGER DEFAULT 0")
            await conn.commit()
            logger.info("Column 'persistent_spam_enabled' added to 'ai_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass  # Column already exists, which is fine
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE bot_users ADD COLUMN is_banned INTEGER DEFAULT 0")
            await conn.commit()
            logger.info("Column 'is_banned' added to 'bot_users' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE ai_settings ADD COLUMN use_proxy_enabled INTEGER DEFAULT 1")
            await conn.commit()
            logger.info("Column 'use_proxy_enabled' added to 'ai_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE ai_settings ADD COLUMN auto_leave_enabled INTEGER DEFAULT 0")
            await conn.commit()
            logger.info("Column 'auto_leave_enabled' added to 'ai_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE bot_users ADD COLUMN role TEXT DEFAULT 'user' NOT NULL")
            await conn.commit()
            logger.info("Column 'role' added to 'bot_users' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise e

        try:
            await conn.execute("ALTER TABLE ai_settings ADD COLUMN attack_skip_admins INTEGER DEFAULT 1")
            await conn.commit()
            logger.info("Column 'attack_skip_admins' added to 'ai_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE spam_media RENAME COLUMN photo_file_id TO photo_file_path")
            await conn.commit()
            logger.info("Column 'photo_file_id' in 'spam_media' renamed to 'photo_file_path'.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e) or "no such column" in str(e):
                pass  # Column already renamed or doesn't exist, which is fine
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN dialogue_simulation_enabled INTEGER DEFAULT 0")
            await conn.commit()
            logger.info("Column 'dialogue_simulation_enabled' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e): pass
            else: raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN dialogue_phrases TEXT")
            await conn.commit()
            logger.info("Column 'dialogue_phrases' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e): pass
            else: raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN dialogues_per_day INTEGER DEFAULT 3")
            await conn.commit()
            logger.info("Column 'dialogues_per_day' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e): pass
            else: raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN active_hours_enabled INTEGER DEFAULT 0")
            await conn.commit()
            logger.info("Column 'active_hours_enabled' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e): pass
            else: raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN active_hours_start INTEGER DEFAULT 9")
            await conn.commit()
            logger.info("Column 'active_hours_start' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e): pass
            else: raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN active_hours_end INTEGER DEFAULT 22")
            await conn.commit()
            logger.info("Column 'active_hours_end' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e): pass
            else: raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings ADD COLUMN inform_user_on_action INTEGER DEFAULT 0")
            await conn.commit()
            logger.info("Column 'inform_user_on_action' added to 'warmer_settings' table.")
        except aiosqlite.OperationalError as e:
            if "duplicate column name" in str(e):
                pass
            else:
                raise e
        try:
            await conn.execute("ALTER TABLE warmer_settings RENAME COLUMN keywords TO target_channels")
            await conn.commit()
            logger.info("Column 'keywords' in 'warmer_settings' renamed to 'target_channels'.")
        except aiosqlite.OperationalError as e:
            if "no such column" in str(e).lower() or "duplicate column" in str(e).lower():
                # Column already renamed or doesn't exist, which is fine
                pass
            else:
                raise e
        await conn.execute('''CREATE TABLE IF NOT EXISTS proxies (
                         user_id INTEGER, proxy_string TEXT,
                         PRIMARY KEY (user_id, proxy_string),
                         FOREIGN KEY (user_id) REFERENCES bot_users(user_id))''')
        await conn.commit()

    async def close(self):
        """Closes the database connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info(f"Database connection for {self.db_name} closed.")

    async def add_bot_user(self, user_id, username=None):
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO bot_users (user_id, username, subscription_until) VALUES (?, ?, NULL)", (user_id, username))
        await conn.commit()

    async def update_delay(self, user_id, delay):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR REPLACE INTO delays (user_id, delay) VALUES (?, ?)", (user_id, delay))
        await conn.commit()

    async def get_delay(self, user_id):
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT delay FROM delays WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        return result[0] if result else config.DEFAULT_DELAY_BETWEEN_COMMENTS

    async def get_user_data(self, user_id):
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT phone, session_file FROM sessions WHERE user_id=?", (user_id,))
            sessions = {row[0]: row[1] for row in await c.fetchall()}
            await c.execute("SELECT chat_identifier FROM chats WHERE user_id=?", (user_id,))
            chats = [row[0] for row in await c.fetchall()]
            await c.execute("SELECT proxy_string FROM proxies WHERE user_id=?", (user_id,))
            proxies = [row[0] for row in await c.fetchall()]
        return {'sessions': sessions, 'chats': chats, 'proxies': proxies}

    async def delete_session_by_filepath(self, user_id, session_file_path):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM sessions WHERE user_id=? AND session_file=?", (user_id, session_file_path))
        await conn.commit()

    async def add_session(self, user_id, phone, session_file_path):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR REPLACE INTO sessions (user_id, phone, session_file) VALUES (?, ?, ?)",
                   (user_id, phone, session_file_path))
        await conn.commit()
        logger.info(f"Session for {phone} added for {user_id} at {session_file_path}")

    async def delete_session(self, user_id, phone):
        session_file_to_delete = None
        conn = await self._get_connection()
        phone_plus = '+' + phone if not phone.startswith('+') else phone

        async with conn.cursor() as cursor:
            # The OR is for legacy compatibility in case a phone was stored without '+'
            await cursor.execute("SELECT session_file FROM sessions WHERE user_id=? AND (phone=? OR phone=?)", (user_id, phone, phone_plus))
            result = await cursor.fetchone()
            if result:
                session_file_to_delete = result[0]

        if not session_file_to_delete:
            logger.warning(f"Attempted to delete session for phone {phone} (user {user_id}), but no DB entry was found.")
            return

        # Delete from DB
        await conn.execute("DELETE FROM sessions WHERE user_id=? AND (phone=? OR phone=?)", (user_id, phone, phone_plus))
        await conn.commit()
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
        conn = await self._get_connection()
        for chat_id_str in chat_identifiers_list:
            clean_chat_id = chat_id_str.strip()
            if not (clean_chat_id.startswith('@') or 't.me/' in clean_chat_id or clean_chat_id.isdigit() or 'joinchat/' in clean_chat_id):
                logger.warning(f"Invalid group identifier format: {clean_chat_id}. Skipping.")
                continue
            await conn.execute("INSERT OR IGNORE INTO chats (user_id, chat_identifier) VALUES (?, ?)",
                         (user_id, clean_chat_id))
        await conn.commit()
        logger.info(f"Chats {chat_identifiers_list} added/updated for {user_id}")

    async def delete_chat(self, user_id, chat_identifier):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM chats WHERE user_id=? AND chat_identifier=?",
                   (user_id, chat_identifier))
        await conn.commit()
        logger.info(f"Chat {chat_identifier} deleted for {user_id}")

    async def update_comments(self, user_id, comment_texts):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("DELETE FROM comments WHERE user_id=?", (user_id,))
        for idx, text in enumerate(comment_texts):
            await conn.execute("INSERT INTO comments (user_id, comment_text, position) VALUES (?, ?, ?)",
                       (user_id, text.strip(), idx))
        await conn.commit()
        logger.info(f"Comments updated for {user_id}. Count: {len(comment_texts)}")

    async def get_comments(self, user_id):
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT comment_text FROM comments WHERE user_id=? ORDER BY position", (user_id,))
            return [row[0] for row in await c.fetchall()]

    async def get_chats_count(self, user_id: int) -> int:
        """Возвращает количество чатов пользователя, не загружая их."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT COUNT(*) FROM chats WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        return result[0] if result else 0

    async def get_paginated_chats(self, user_id: int, page: int, page_size: int) -> list[str]:
        """Возвращает определенную страницу из списка чатов пользователя."""
        conn = await self._get_connection()
        offset = (page - 1) * page_size
        async with conn.cursor() as c:
            await c.execute("SELECT chat_identifier FROM chats WHERE user_id=? ORDER BY rowid LIMIT ? OFFSET ?", (user_id, page_size, offset))
            return [row[0] for row in await c.fetchall()]

    async def get_chats_stream(self, user_id: int):
        """Асинхронно отдает идентификаторы чатов по одному для экономии памяти."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT chat_identifier FROM chats WHERE user_id=?", (user_id,))
            async for row in c:
                yield row[0]

    async def reset_sessions(self, user_id):
        conn = await self._get_connection()
        # First, get all session file paths for the user
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT session_file FROM sessions WHERE user_id=?", (user_id,))
            session_files_to_delete = [row[0] for row in await cursor.fetchall()]

        # Then, delete from DB
        await conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        await conn.commit()
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
        conn = await self._get_connection()
        await conn.execute("DELETE FROM chats WHERE user_id=?", (user_id,))
        await conn.commit()
        logger.info(f"All chats for {user_id} have been deleted.")

    async def reset_comments(self, user_id):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM comments WHERE user_id=?", (user_id,))
        await conn.commit()
        await self.delete_spam_photo(user_id)
        logger.info(f"All comments and spam media for user {user_id} have been deleted.")

    async def reset_scraped_users(self, user_id):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM scraped_users WHERE user_id=?", (user_id,))
        await conn.commit()
        logger.info(f"All scraped users for {user_id} have been deleted.")

    async def add_proxy(self, user_id, proxy_string):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO proxies (user_id, proxy_string) VALUES (?, ?)", (user_id, proxy_string))
        await conn.commit()
        logger.info(f"Proxy {proxy_string} added for {user_id}")

    async def delete_proxy(self, user_id, proxy_string):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM proxies WHERE user_id=? AND proxy_string=?", (user_id, proxy_string))
        await conn.commit()
        logger.info(f"Proxy {proxy_string} deleted for {user_id}")

    async def get_proxies(self, user_id):
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT proxy_string FROM proxies WHERE user_id=?", (user_id,))
            return [row[0] for row in await c.fetchall()]

    async def reset_proxies(self, user_id):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM proxies WHERE user_id=?", (user_id,))
        await conn.commit()
        logger.info(f"All proxies for {user_id} have been deleted.")

    # --- AI Settings Methods ---
    async def set_gemini_api_key(self, user_id, api_key):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET gemini_api_key = ? WHERE user_id = ?", (api_key, user_id))
        await conn.commit()
        logger.info(f"Gemini API key set for user {user_id}")

    async def set_uniqueness_prompt(self, user_id, prompt):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET uniqueness_prompt = ? WHERE user_id = ?", (prompt, user_id))
        await conn.commit()
        logger.info(f"Uniqueness prompt set for user {user_id}")

    async def set_uniqueness_enabled(self, user_id, enabled: bool):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET uniqueness_enabled = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
        await conn.commit()
        logger.info(f"Gemini uniqueness {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_proxy_enabled(self, user_id, enabled: bool):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET use_proxy_enabled = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
        await conn.commit()
        logger.info(f"Proxy usage {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_persistent_spam_enabled(self, user_id, enabled: bool):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET persistent_spam_enabled = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
        await conn.commit()
        logger.info(f"Persistent spam {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_auto_leave_enabled(self, user_id, enabled: bool):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET auto_leave_enabled = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
        await conn.commit()
        logger.info(f"Auto-leave groups {'enabled' if enabled else 'disabled'} for {user_id}")

    async def set_attack_skip_admins(self, user_id, enabled: bool):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO ai_settings (user_id) VALUES (?)", (user_id,))
        await conn.execute("UPDATE ai_settings SET attack_skip_admins = ? WHERE user_id = ?", (1 if enabled else 0, user_id))
        await conn.commit()
        logger.info(f"Attack skip admins {'enabled' if enabled else 'disabled'} for {user_id}")

    async def get_ai_settings(self, user_id: int) -> dict:
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT gemini_api_key, uniqueness_enabled, uniqueness_prompt, persistent_spam_enabled, use_proxy_enabled, auto_leave_enabled, attack_skip_admins FROM ai_settings WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        if result:
            return {
                "api_key": result[0],
                "enabled": bool(result[1]),
                "prompt": result[2] if result[2] else config.DEFAULT_GEMINI_PROMPT,
                "persistent_spam": bool(result[3]),
                "use_proxy": bool(result[4]) if result[4] is not None else True,
                "auto_leave_enabled": bool(result[5]),
                "attack_skip_admins": bool(result[6]) if result[6] is not None else True
            }
        return {"api_key": None, "enabled": False, "prompt": config.DEFAULT_GEMINI_PROMPT, "persistent_spam": False, "use_proxy": True, "auto_leave_enabled": False, "attack_skip_admins": True}

    # --- Scraped Users Methods ---
    async def add_scraped_users(self, user_id: int, source_group: str, users: list[dict]):
        """Adds a list of scraped users to the database."""
        conn = await self._get_connection()
        now = datetime.now(timezone.utc)
        data_to_insert = [
            (user_id, user['id'], user.get('username'), source_group, now)
            for user in users
        ]
        await conn.executemany(
            "INSERT OR REPLACE INTO scraped_users (user_id, scraped_user_id, username, source_group, scraped_at) VALUES (?, ?, ?, ?, ?)",
            data_to_insert
        )
        await conn.commit()
        logger.info(f"Added/updated {len(data_to_insert)} scraped users for user {user_id} from {source_group}.")

    async def get_scraped_users_count(self, user_id: int) -> int:
        """Returns the count of scraped users for a user."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT COUNT(*) FROM scraped_users WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        return result[0] if result else 0

    async def get_scraped_users_stream(self, user_id: int):
        """Asynchronously yields scraped user IDs."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT scraped_user_id FROM scraped_users WHERE user_id=?", (user_id,))
            async for row in c:
                yield row[0]

    # --- Warmer Settings Methods ---
    async def get_warmer_settings(self, user_id: int) -> dict:
        """Gets warmer settings for a user, returning defaults if not set."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT duration_days, join_channels_per_day, send_reactions_per_day, target_channels, inform_user_on_action, dialogue_simulation_enabled, dialogue_phrases, dialogues_per_day, active_hours_enabled, active_hours_start, active_hours_end FROM warmer_settings WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        if result:
            return {
                "duration_days": result[0],
                "join_channels_per_day": result[1],
                "send_reactions_per_day": result[2],
                "target_channels": result[3] if result[3] else "",
                "inform_user_on_action": bool(result[4]),
                "dialogue_simulation_enabled": bool(result[5]),
                "dialogue_phrases": result[6] if result[6] else "Привет,Как дела?,Что нового?",
                "dialogues_per_day": result[7],
                "active_hours_enabled": bool(result[8]),
                "active_hours_start": result[9],
                "active_hours_end": result[10],
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
        conn = await self._get_connection()
        await conn.execute("INSERT OR IGNORE INTO warmer_settings (user_id) VALUES (?)", (user_id,))
        
        for key, value in settings.items():
            # This is safe as we control the keys
            query = f"UPDATE warmer_settings SET {key} = ? WHERE user_id = ?"
            await conn.execute(query, (value, user_id))
        
        await conn.commit()
        logger.info(f"Warmer settings updated for user {user_id} with {settings}")

    # --- Scheduler Methods ---
    async def add_scheduled_task(self, job_id: str, user_id: int, task_type: str, cron_expression: str, task_params: str):
        """Adds a new scheduled task to the database."""
        conn = await self._get_connection()
        now = datetime.now(timezone.utc)
        await conn.execute(
            "INSERT INTO scheduled_tasks (job_id, user_id, task_type, task_params, cron_expression, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, user_id, task_type, task_params, cron_expression, now)
        )
        await conn.commit()
        logger.info(f"Scheduled task {job_id} of type {task_type} added for user {user_id}.")

    async def get_active_scheduled_tasks(self) -> list[dict]:
        """Gets all active scheduled tasks to load into the scheduler on startup."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT job_id, user_id, task_type, task_params, cron_expression FROM scheduled_tasks WHERE is_active = 1")
            rows = await c.fetchall()
        return [{"job_id": r[0], "user_id": r[1], "task_type": r[2], "task_params": r[3], "cron": r[4]} for r in rows]

    async def get_scheduled_tasks_for_user(self, user_id: int) -> list[dict]:
        """Gets all scheduled tasks for a specific user to display in the menu."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT job_id, task_type, cron_expression FROM scheduled_tasks WHERE user_id = ? ORDER BY created_at DESC", (user_id,))
            rows = await c.fetchall()
        return [{"job_id": r[0], "task_type": r[1], "cron": r[2]} for r in rows]

    async def remove_scheduled_task(self, job_id: str):
        """Removes a scheduled task from the database by its ID."""
        conn = await self._get_connection()
        await conn.execute("DELETE FROM scheduled_tasks WHERE job_id = ?", (job_id,))
        await conn.commit()
        logger.info(f"Scheduled task {job_id} removed from the database.")

    # --- Spam Media Methods ---
    async def set_spam_photo(self, user_id: int, photo_file_path: str):
        """Saves or updates the file_path of the photo for spamming."""
        await self.add_bot_user(user_id)
        # Before setting a new photo, delete the old one if it exists.
        old_path = await self.get_spam_photo(user_id)
        if old_path and os.path.exists(old_path) and old_path != photo_file_path:
            try:
                os.remove(old_path)
                logger.info(f"Old spam photo at {old_path} deleted for user {user_id}.")
            except OSError as e:
                logger.error(f"Error deleting old spam photo {old_path}: {e}")

        conn = await self._get_connection()
        await conn.execute("INSERT OR REPLACE INTO spam_media (user_id, photo_file_path) VALUES (?, ?)", (user_id, photo_file_path))
        await conn.commit()
        logger.info(f"Spam photo path set for user {user_id}")

    async def get_spam_photo(self, user_id: int) -> Optional[str]:
        """Gets the file_path of the photo for spamming."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT photo_file_path FROM spam_media WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        return result[0] if result and result[0] else None

    async def delete_spam_photo(self, user_id: int):
        """Deletes the photo for spamming from DB and filesystem."""
        photo_path = await self.get_spam_photo(user_id)
        conn = await self._get_connection()
        await conn.execute("DELETE FROM spam_media WHERE user_id=?", (user_id,))
        await conn.commit()
        if photo_path and os.path.exists(photo_path):
            try:
                os.remove(photo_path)
                logger.info(f"Spam photo file {photo_path} deleted for user {user_id}.")
            except OSError as e:
                logger.error(f"Error deleting spam photo file {photo_path}: {e}")

    # --- Subscription and Admin Methods ---
    async def set_ban_status(self, user_id: int, ban_status: bool):
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("UPDATE bot_users SET is_banned = ? WHERE user_id = ?", (1 if ban_status else 0, user_id))
        await conn.commit()
        logger.info(f"User {user_id} ban status set to {ban_status}")

    async def grant_subscription(self, user_id: int, days: int) -> datetime:
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        status = await self.get_subscription_status(user_id)
        current_expiry = status['expires_at']
        
        now = datetime.now()
        start_date = current_expiry if current_expiry and current_expiry > now else now
        new_expiry_date = start_date + timedelta(days=days)
        
        await conn.execute("UPDATE bot_users SET subscription_until = ? WHERE user_id = ?", (new_expiry_date, user_id))
        await conn.commit()
        logger.info(f"Subscription for user {user_id} extended by {days} days. New expiry: {new_expiry_date}")
        return new_expiry_date

    async def get_subscription_status(self, user_id: int) -> dict:
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT subscription_until, is_banned FROM bot_users WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        
        if not result:
            return {"active": False, "expires_at": None, "is_banned": False}

        sub_until_str, is_banned_int = result
        expires_at = datetime.fromisoformat(sub_until_str) if sub_until_str else None
        is_active = expires_at > datetime.now() if expires_at else False

        return {"active": is_active, "expires_at": expires_at, "is_banned": bool(is_banned_int)}

    async def get_user_role(self, user_id: int) -> str:
        if user_id == config.SUPER_ADMIN_ID:
            return 'super_admin'
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT role FROM bot_users WHERE user_id=?", (user_id,))
            result = await c.fetchone()
        # Возвращаем 'user' по умолчанию, если не найден или роль NULL
        return result[0] if result and result[0] else 'user'

    async def set_user_role(self, user_id: int, role: str):
        if role not in ['user', 'admin']:
            logger.warning(f"Попытка установить неверную роль '{role}' для пользователя {user_id}. Отмена.")
            return
        # Роль суперадмина неявная и не может быть установлена в БД
        if user_id == config.SUPER_ADMIN_ID:
            logger.warning(f"Попытка изменить роль для SUPER_ADMIN_ID {user_id}. Отмена.")
            return
        await self.add_bot_user(user_id)
        conn = await self._get_connection()
        await conn.execute("UPDATE bot_users SET role = ? WHERE user_id = ?", (role, user_id))
        await conn.commit()
        logger.info(f"Роль пользователя {user_id} установлена на '{role}'")

    async def get_all_admins(self) -> list[dict]:
        conn = await self._get_connection()
        # Сначала добавляем суперадмина
        admins = [{'user_id': config.SUPER_ADMIN_ID, 'role': 'super_admin', 'username': 'N/A'}]
        async with conn.cursor() as c:
            await c.execute("SELECT user_id, username FROM bot_users WHERE role='admin'")
            db_admins = await c.fetchall()
            for admin_id, username in db_admins:
                admins.append({'user_id': admin_id, 'role': 'admin', 'username': username})
        return admins

    async def get_all_user_ids(self) -> list[int]:
        """Returns a list of all non-banned user IDs from the bot_users table."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            # --- ИЗМЕНЕНО: Добавлено условие для исключения забаненных пользователей ---
            await c.execute("SELECT user_id FROM bot_users WHERE is_banned = 0")
            rows = await c.fetchall()
        return [row[0] for row in rows]

    async def get_bot_stats(self) -> dict:
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT COUNT(*) FROM bot_users")
            total_users = (await c.fetchone())[0]
            
            now = datetime.now()
            await c.execute("SELECT COUNT(*) FROM bot_users WHERE subscription_until > ?", (now,))
            active_subscriptions = (await c.fetchone())[0]
            
        return {"total_users": total_users, "active_subscriptions": active_subscriptions}

    # --- Promo Code Methods ---
    async def create_promo_code(self, code: str, days: int, max_activations: int):
        conn = await self._get_connection()
        now = datetime.now()
        await conn.execute(
            "INSERT INTO promo_codes (code, duration_days, max_activations, created_at) VALUES (?, ?, ?, ?)",
            (code, days, max_activations, now)
        )
        await conn.commit()
        logger.info(f"Promo code '{code}' for {days} days created with {max_activations} max activations.")

    async def get_promo_code_details(self, code: str) -> dict | None:
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT duration_days, max_activations FROM promo_codes WHERE code=?", (code,))
            promo_result = await c.fetchone()
            if not promo_result:
                return None

            await c.execute("SELECT user_id, activated_at FROM promo_code_activations WHERE promo_code=?", (code,))
            activations = await c.fetchall()

        return {
            "code": code,
            "duration_days": promo_result[0],
            "max_activations": promo_result[1],
            "current_activations": len(activations),
            "activations": [{"user_id": r[0], "activated_at": datetime.fromisoformat(r[1])} for r in activations]
        }

    async def has_user_activated_code(self, code: str, user_id: int) -> bool:
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT 1 FROM promo_code_activations WHERE promo_code=? AND user_id=?", (code, user_id))
            return await c.fetchone() is not None

    async def activate_promo_code(self, code: str, user_id: int):
        conn = await self._get_connection()
        now = datetime.now()
        await conn.execute(
            "INSERT INTO promo_code_activations (promo_code, user_id, activated_at) VALUES (?, ?, ?)",
            (code, user_id, now)
        )
        await conn.commit()
        logger.info(f"Promo code '{code}' activated by user {user_id}.")

    async def get_all_promo_codes_details(self) -> list[dict]:
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT p.code, p.duration_days, p.max_activations, COUNT(a.id) FROM promo_codes p LEFT JOIN promo_code_activations a ON p.code = a.promo_code GROUP BY p.code ORDER BY p.created_at DESC")
            return [{"code": r[0], "duration_days": r[1], "max_activations": r[2], "current_activations": r[3]} for r in await c.fetchall()]

    async def delete_promo_code(self, code: str):
        conn = await self._get_connection()
        await conn.execute("DELETE FROM promo_codes WHERE code=?", (code,))
        await conn.commit()
        logger.info(f"Promo code '{code}' deleted.")

    # --- Bot Settings ---
    async def get_bot_setting(self, key: str) -> Optional[str]:
        """Gets a global bot setting from the database."""
        conn = await self._get_connection()
        async with conn.cursor() as c:
            await c.execute("SELECT value FROM bot_settings WHERE key=?", (key,))
            result = await c.fetchone()
        return result[0] if result else None

    async def set_bot_setting(self, key: str, value: str):
        """Sets a global bot setting in the database."""
        conn = await self._get_connection()
        await conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
        await conn.commit()

# Создаем один экземпляр менеджера для использования во всем приложении
db_manager = DatabaseManager(config.DB_NAME)