#!/usr/bin/env python3
"""podcast.py — совместимость для бота: вся обработка живёт в пакете podcast_core (папка core/).

`import podcast` отдаёт сам модуль podcast_core.pipeline (а не копию), поэтому `P.log = …` в bot.py
и подмены в тестах работают как раньше. Общие settings.json и assets/ — рядом с этим файлом (/app в образе).
CLI тот же: python3 podcast.py run|render|analyze …
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
try:
    import podcast_core  # noqa: F401  (в образе: pip install core/)
except ImportError:      # в папке с исходниками — без установки
    sys.path.insert(0, str(HERE / "core" / "src"))
from podcast_core import pipeline  # noqa: E402

pipeline.configure(HERE, title="podcast-bot", lang="ru")   # бот говорит по-русски
if __name__ == "__main__":
    pipeline.main()
else:
    sys.modules[__name__] = pipeline
