import argparse
import os

import uvicorn

from .app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Creator Archive local workspace")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="Independent local data directory")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["CREATOR_ARCHIVE_DATA_DIR"] = args.data_dir
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, access_log=False))
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
