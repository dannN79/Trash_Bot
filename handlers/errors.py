from aiogram import Router
from aiogram.types import ErrorEvent
import logging

router = Router()

@router.errors()
async def error_handler(event: ErrorEvent):
    logging.error(f"Ошибка: {event.exception}")
    return True