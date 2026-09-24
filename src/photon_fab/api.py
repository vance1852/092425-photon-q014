"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from .service import AUDIT_PAGE_DEFAULT, PhotonService


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "lots":
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                return self._json(200, self.service.get_lot(token, parts[1]))
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        if len(parts) == 3 and parts[0] == "lots" and parts[2] == "audit":
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                query = parse_qs(parsed.query)
                cursor = query.get("cursor", [None])[0]
                limit = int(query.get("limit", [str(AUDIT_PAGE_DEFAULT)])[0])
                return self._json(200, self.service.export_audit(token, parts[1], cursor, limit))
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                return self._json(200, self.service.analyze(token, self.path.split("/")[2]))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    # 单线程服务：SQLite 连接与请求处理保持同一线程
    HTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
