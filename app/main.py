"""Entry point: python -m app.main"""

import logging
import shutil
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from .config import load_settings


def main() -> int:
    logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token URL
    try:
        settings = load_settings()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not shutil.which("docker"):
        print("Docker is required for the sandbox (see README).", file=sys.stderr)
        return 2
    import anthropic

    from .handlers.telegram import build_app

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    print(f"Daedalus running with {settings.model}; allowed users: {sorted(settings.allowed_user_ids)}")
    build_app(settings, client).run_polling()
    return 0


if __name__ == "__main__":
    sys.exit(main())
