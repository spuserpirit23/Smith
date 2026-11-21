"""Run the backend with an event loop that works for MQTT on Windows.

    python -m mitra.serve                     # = uvicorn mitra.main:app_factory --factory
    python -m mitra.serve --port 8080 --env-file .env --host 0.0.0.0

Why: aiomqtt (camera / device shadow over MQTT) needs ``loop.add_reader``. Windows' default
ProactorEventLoop doesn't have it, and uvicorn 0.34 only switches to a SelectorEventLoop in
reload/worker mode, so a plain ``uvicorn ...`` start leaves MQTT broken on Windows. Elsewhere this
is identical to running uvicorn directly.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import warnings


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m mitra.serve")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args(argv)

    if sys.platform == "win32":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)   # policy API deprecated in 3.14
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import uvicorn

    uvicorn.run("mitra.main:app_factory", factory=True, host=a.host, port=a.port, env_file=a.env_file,
                loop="asyncio")


if __name__ == "__main__":
    main()
