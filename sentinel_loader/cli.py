from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SentinelLoader — parse CSV/JSON logs and ingest them into Microsoft Sentinel."
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Port (default 8080)")
    args = parser.parse_args()
    uvicorn.run("sentinel_loader.app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
