import argparse
import os

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="Start GeoBIM Sheet and Revision Review Agent.")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    print(f"Open http://{args.host}:{args.port} (Sheet) or http://{args.host}:{args.port}/agent (Review Agent)", flush=True)
    create_app().run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
