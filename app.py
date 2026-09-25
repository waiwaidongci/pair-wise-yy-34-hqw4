from __future__ import annotations
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
from src.accident_http import AccidentRouter
from src.accident_repository import AccidentRepository
from src.accident_service import AccidentService
from src.http_api import make_handler
from src.repository import Repository
from src.service import Service


def parse_args():
    parser = argparse.ArgumentParser(description="工伤事故调查与重复事故合并")
    parser.add_argument("--db", default="./data.db", help="调查流程SQLite数据库路径")
    parser.add_argument("--accident-db", default="./accidents.db",
                        help="重复事故合并SQLite数据库路径")
    parser.add_argument("--port", type=int, default=8311, help="HTTP端口")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    return parser.parse_args()


def main():
    args = parse_args()
    repository = Repository(args.db)
    service = Service(repository)
    accident_repository = AccidentRepository(args.accident_db)
    accident_service = AccidentService(accident_repository)
    accident_router = AccidentRouter(accident_service)
    server = ThreadingHTTPServer(
        (args.host, args.port),
        make_handler(service, str(Path(__file__).resolve().parent / "static"),
                     accident_router))
    print(f"listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        repository.close()
        accident_repository.close()


if __name__ == "__main__":
    main()
