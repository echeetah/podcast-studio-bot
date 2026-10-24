"""Общие настройки тестов: путь к модулям проекта и окружение бота (без сети и настоящих секретов)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("PEOPLE", "111:Аня:Europe/Lisbon,222:Боря:Asia/Tbilisi")
os.environ.setdefault("BOT_API", "http://127.0.0.1:9")
