# bot/utils/gemini.py
import logging

from google.api_core import exceptions as google_exceptions
import google.generativeai as genai

import config

logger = logging.getLogger(__name__)

async def get_unique_text_gemini(original_text: str, api_key: str, custom_prompt: str) -> tuple[str | None, str | None]:
    """
    Asynchronously gets a unique version of the text using Google Gemini API.
    Returns a tuple: (unique_text, error_message).
    On success, error_message is None.
    On failure, unique_text is None.
    """
    if not api_key:
        msg = "API ключ Gemini не установлен"
        logger.warning(f"{msg}. Уникализация невозможна.")
        return None, msg
    if not original_text:
        return original_text, None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(config.GEMINI_MODEL_NAME)
        full_prompt = f"{custom_prompt.strip()} \"{original_text}\""

        logger.info(f"Асинхронный запрос к Gemini: {full_prompt[:100]}...")
        response = await model.generate_content_async(full_prompt)

        if response.parts:
            unique_text = response.text.strip()
            logger.info(f"Gemini вернул: {unique_text[:100]}...")
            return unique_text, None
        else:
            feedback = "Причина не указана API."
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback.block_reason:
                 feedback = f"Контент заблокирован: {response.prompt_feedback.block_reason.name}"
            logger.warning(f"Gemini не вернул текст. {feedback}")
            return None, feedback
    except google_exceptions.ResourceExhausted:
        error_msg = "Исчерпана квота Gemini API"
        logger.error(f"Ошибка при запросе к Gemini API: {error_msg}")
        return None, error_msg
    except google_exceptions.PermissionDenied:
        error_msg = "Неверный API ключ Gemini"
        logger.error(f"Ошибка при запросе к Gemini API: {error_msg}")
        return None, error_msg
    except Exception as e:
        error_msg = f"Ошибка Gemini API ({type(e).__name__})"
        logger.error(f"Ошибка при запросе к Gemini API: {e}", exc_info=True)
        return None, error_msg