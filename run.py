"""Start the app: one process, web UI plus the background sender thread."""
from __future__ import annotations

import os
import threading
import webbrowser

HOST = os.environ.get("SENDER_HOST", "127.0.0.1")
PORT = int(os.environ.get("SENDER_PORT", "8420"))


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    import uvicorn

    url = f"http://{HOST}:{PORT}"
    print(f"Email Sender Bot -> {url}")
    print("Press Ctrl+C to stop. Sending is OFF until you start it in the UI.")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.main:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
