#!/usr/bin/env python3
"""mBot2 3Dシミュレーターの保存サーバー (Python 標準ライブラリだけで動きます)。

使い方:
    python server.py                       このパソコンの中だけで動かす (http://127.0.0.1:8080/)
    python server.py --host 0.0.0.0        校内の他のパソコンからもつなげる (下の注意を読んでください)
    python server.py --port 9000 --db /var/lib/mbot2/programs.db

- シミュレーターの画面 (mbot2-3d-sim.html) も、このサーバーから配ります。
- プログラム (ブロックの XML) は SQLite のファイル (既定: programs.db) に保存します。
- 認証はありません。校内で使う場合は、学校のネットワークの中だけにとどめ、
  必要になったらログインの仕組みを足してください。
"""
import argparse
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
# 配ってよいファイルは、ここに書いたものだけです (それ以外は返しません)
STATIC = {"/": "mbot2-3d-sim.html", "/index.html": "mbot2-3d-sim.html", "/mbot2-3d-sim.html": "mbot2-3d-sim.html"}
MAX_BODY = 1_000_000        # 1リクエストの最大サイズ (バイト)
MAX_NAME = 60
POSES = ("line", "center")


def db_connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def db_init(path):
    with db_connect(path) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS programs ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name TEXT NOT NULL UNIQUE,"
            " xml TEXT NOT NULL,"
            " start_pose TEXT NOT NULL DEFAULT 'line',"
            " created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL)"
        )


class Server(ThreadingHTTPServer):
    # Windows では、既定のままだと同じポートで二重に起動できてしまうので、禁止する
    allow_reuse_address = os.name != "nt"


class Handler(BaseHTTPRequestHandler):
    server_version = "mbot2-sim-server"
    db_path = "programs.db"

    # ---- 共通 ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        self._send(code, {"error": message})

    def _same_origin(self):
        """別のサイトの画面から勝手に書き込まれないよう、Origin を確かめる"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return origin in ("http://" + host, "https://" + host)

    def _read_json(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._error(415, "Content-Type は application/json にしてください")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            self._error(413, "データの大きさが正しくありません (上限 %d バイト)" % MAX_BODY)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._error(400, "JSON を読み取れませんでした")
            return None
        if not isinstance(data, dict):
            self._error(400, "JSON の形が正しくありません")
            return None
        return data

    def _program_id(self, path):
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "programs"] and parts[2].isdigit():
            return int(parts[2])
        return None

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/programs":
            with db_connect(self.db_path) as con:
                rows = con.execute("SELECT id, name, start_pose, updated_at FROM programs ORDER BY updated_at DESC").fetchall()
            return self._send(200, [dict(r) for r in rows])
        pid = self._program_id(path)
        if pid is not None:
            with db_connect(self.db_path) as con:
                row = con.execute("SELECT id, name, xml, start_pose, updated_at FROM programs WHERE id = ?", (pid,)).fetchone()
            return self._send(200, dict(row)) if row else self._error(404, "見つかりません")
        if path in STATIC:
            try:
                with open(os.path.join(BASE, STATIC[path]), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._error(404, "画面のファイルが見つかりません")
        if path == "/favicon.ico":
            return self._send(204)
        self._error(404, "見つかりません。ブラウザでは「http://%s/」を開いてください" % (self.headers.get("Host") or "127.0.0.1:8080"))

    # ---- POST (保存。同じ名前があれば上書き) ----
    def do_POST(self):
        if urlparse(self.path).path != "/api/programs":
            return self._error(404, "見つかりません")
        if not self._same_origin():
            return self._error(403, "別のサイトからは書き込めません")
        data = self._read_json()
        if data is None:
            return
        name = data.get("name")
        xml = data.get("xml")
        pose = data.get("startPose", "line")
        if not isinstance(name, str) or not (1 <= len(name.strip()) <= MAX_NAME):
            return self._error(400, "名前は1〜%d文字で入力してください" % MAX_NAME)
        if not isinstance(xml, str) or not xml.lstrip().startswith("<xml"):
            return self._error(400, "ブロックのデータの形が正しくありません")
        if pose not in POSES:
            return self._error(400, "スタート位置が正しくありません")
        name = name.strip()
        now = time.time()
        with db_connect(self.db_path) as con:
            row = con.execute("SELECT id FROM programs WHERE name = ?", (name,)).fetchone()
            if row:
                con.execute("UPDATE programs SET xml = ?, start_pose = ?, updated_at = ? WHERE id = ?", (xml, pose, now, row["id"]))
                return self._send(200, {"id": row["id"], "updated": True})
            cur = con.execute("INSERT INTO programs (name, xml, start_pose, created_at, updated_at) VALUES (?, ?, ?, ?, ?)", (name, xml, pose, now, now))
            return self._send(201, {"id": cur.lastrowid, "updated": False})

    # ---- DELETE ----
    def do_DELETE(self):
        if not self._same_origin():
            return self._error(403, "別のサイトからは削除できません")
        pid = self._program_id(urlparse(self.path).path)
        if pid is None:
            return self._error(404, "見つかりません")
        with db_connect(self.db_path) as con:
            cur = con.execute("DELETE FROM programs WHERE id = ?", (pid,))
        self._send(200, {"deleted": cur.rowcount})

    def log_message(self, fmt, *args):
        print("%s  %s" % (time.strftime("%H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser(description="mBot2 シミュレーターの保存サーバー")
    ap.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス (既定: 127.0.0.1 = このパソコンだけ)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=os.path.join(BASE, "programs.db"), help="SQLite のファイル")
    args = ap.parse_args()
    db_init(args.db)
    Handler.db_path = args.db
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        raise SystemExit("起動できませんでした (%s)。すでに別の server.py が動いていないか、ポート %d が使われていないか確認してください。" % (e, args.port))
    print("保存サーバーを起動しました:  http://%s:%d/   (データ: %s)   止めるには Ctrl+C" % (args.host, args.port, args.db))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n止めました")


if __name__ == "__main__":
    main()
