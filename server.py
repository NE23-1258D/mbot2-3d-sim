#!/usr/bin/env python3
"""mBot2 3Dシミュレーターの保存サーバー (Python 標準ライブラリだけで動きます)。

ログインは要りません。プログラムを保存すると 6 桁の ID が返ります。
あとからその ID を入れると、同じプログラムを呼び出せます。

使い方:
    python server.py                       このパソコンの中だけで動かす (http://127.0.0.1:8080/)
    python server.py --host 0.0.0.0        校内の他のパソコンからもつなげる
    python server.py --db /var/lib/mbot2/programs.db --keep-days 180

管理 (サーバーを止めずに、別のターミナルで実行できます):
    python server.py --admin-count         保存されている件数
    python server.py --admin-delete 123456 ID を指定して削除

注意:
- ID を知っている人は誰でも読み込めます。他の人に見せたくない内容は保存しないでください。
- 6 桁の ID は、当てずっぽうで探されないよう、間違いが続くと一時的に止めます。
- プログラムは、既定で 180 日たつと自動で消えます (--keep-days 0 で消さない)。
"""
import argparse
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
# 配ってよいファイルは、ここに書いたものだけです (それ以外は返しません)
STATIC = {"/": "mbot2-3d-sim.html", "/index.html": "mbot2-3d-sim.html", "/mbot2-3d-sim.html": "mbot2-3d-sim.html"}
MAX_BODY = 300_000          # 1リクエストの最大サイズ (バイト)。ブロックのデータは数KB〜数十KB
POSES = ("line", "center")
CODE_RE = re.compile(r"[1-9][0-9]{5}")    # 100000〜999999

# 悪用への備え
FAIL_LIMIT, FAIL_WINDOW = 15, 60          # 1分間に15回まちがえたら、その接続元を止める
SAVE_LIMIT, SAVE_WINDOW = 20, 600         # 10分間に20回まで保存できる
MAX_ITEMS = 20000                         # 保存できる最大件数

CONFIG = {"db": os.path.join(BASE, "programs.db"), "keep_days": 180}


class Limiter:
    """接続元ごとの回数を数える (メモリの中だけ。サーバーを止めると忘れます)"""

    def __init__(self):
        self.lock = threading.Lock()
        self.events = {}

    def count(self, key, window):
        now = time.time()
        with self.lock:
            q = [t for t in self.events.get(key, []) if now - t < window]
            self.events[key] = q
            return len(q)

    def add(self, key, window):
        now = time.time()
        with self.lock:
            q = [t for t in self.events.get(key, []) if now - t < window]
            q.append(now)
            self.events[key] = q
            if len(self.events) > 5000:     # 古い接続元の記録を片づける
                for k in [k for k, v in self.events.items() if not v or now - v[-1] > 3600]:
                    self.events.pop(k, None)


LIMITER = Limiter()


def db_connect():
    con = sqlite3.connect(CONFIG["db"])
    con.row_factory = sqlite3.Row
    return con


def db_init():
    with db_connect() as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS saved ("
            " code TEXT PRIMARY KEY,"
            " xml TEXT NOT NULL,"
            " start_pose TEXT NOT NULL DEFAULT 'line',"
            " created_at REAL NOT NULL)"
        )


def purge_old():
    days = CONFIG["keep_days"]
    if days > 0:
        with db_connect() as con:
            con.execute("DELETE FROM saved WHERE created_at < ?", (time.time() - days * 86400,))


class Server(ThreadingHTTPServer):
    # Windows では、既定のままだと同じポートで二重に起動できてしまうので、禁止する
    allow_reuse_address = os.name != "nt"


class Handler(BaseHTTPRequestHandler):
    server_version = "mbot2-sim-server"

    # ---- 共通 ----
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message, extra=None):
        self._send(code, {"error": message}, extra=extra)

    def _ip(self):
        return self.client_address[0]

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

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/ping":
            return self._send(200, {"ok": True})
        m = re.fullmatch(r"/api/programs/([^/]+)", path)
        if m:
            return self._load(m.group(1))
        if path in STATIC:
            try:
                with open(os.path.join(BASE, STATIC[path]), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._error(404, "画面のファイルが見つかりません")
        if path == "/favicon.ico":
            return self._send(204)
        self._error(404, "見つかりません。ブラウザでは「http://%s/」を開いてください" % (self.headers.get("Host") or "127.0.0.1:8080"))

    def _load(self, code):
        ip = self._ip()
        if LIMITER.count("fail:" + ip, FAIL_WINDOW) >= FAIL_LIMIT:
            return self._error(429, "まちがいが続いたため、しばらく止めています。1分ほど待ってからもう一度お試しください", {"Retry-After": str(FAIL_WINDOW)})
        row = None
        if CODE_RE.fullmatch(code):
            with db_connect() as con:
                row = con.execute("SELECT code, xml, start_pose, created_at FROM saved WHERE code = ?", (code,)).fetchone()
        if not row:
            LIMITER.add("fail:" + ip, FAIL_WINDOW)
            return self._error(404, "その ID のプログラムは見つかりません")
        self._send(200, dict(row))

    # ---- POST (保存: 新しい ID を返す) ----
    def do_POST(self):
        if urlparse(self.path).path != "/api/programs":
            return self._error(404, "見つかりません")
        if not self._same_origin():
            return self._error(403, "別のサイトからは書き込めません")
        ip = self._ip()
        if LIMITER.count("save:" + ip, SAVE_WINDOW) >= SAVE_LIMIT:
            return self._error(429, "保存が続いたため、しばらく止めています。少し待ってからもう一度お試しください", {"Retry-After": "60"})
        data = self._read_json()
        if data is None:
            return
        xml = data.get("xml")
        pose = data.get("startPose", "line")
        if not isinstance(xml, str) or not xml.lstrip().startswith("<xml"):
            return self._error(400, "ブロックのデータの形が正しくありません")
        if pose not in POSES:
            return self._error(400, "スタート位置が正しくありません")
        purge_old()
        with db_connect() as con:
            if con.execute("SELECT COUNT(*) FROM saved").fetchone()[0] >= MAX_ITEMS:
                return self._error(507, "サーバーがいっぱいです。管理者に連絡してください")
            for _ in range(50):
                code = str(secrets.randbelow(900000) + 100000)
                try:
                    con.execute("INSERT INTO saved (code, xml, start_pose, created_at) VALUES (?, ?, ?, ?)", (code, xml, pose, time.time()))
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                return self._error(500, "ID を作れませんでした。もう一度お試しください")
        LIMITER.add("save:" + ip, SAVE_WINDOW)
        self._send(201, {"code": code})

    def log_message(self, fmt, *args):
        print("%s  %s" % (time.strftime("%H:%M:%S"), fmt % args))


def main():
    ap = argparse.ArgumentParser(description="mBot2 シミュレーターの保存サーバー")
    ap.add_argument("--host", default="127.0.0.1", help="待ち受けるアドレス (既定: 127.0.0.1 = このパソコンだけ)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=CONFIG["db"], help="SQLite のファイル")
    ap.add_argument("--keep-days", type=int, default=CONFIG["keep_days"], help="保存しておく日数 (0 で消さない)")
    ap.add_argument("--admin-count", action="store_true", help="保存されている件数を表示して終了")
    ap.add_argument("--admin-delete", metavar="ID", help="6桁の ID を指定して削除して終了")
    args = ap.parse_args()
    CONFIG["db"], CONFIG["keep_days"] = args.db, max(0, args.keep_days)
    db_init()

    if args.admin_count:
        with db_connect() as con:
            print("保存されている件数:", con.execute("SELECT COUNT(*) FROM saved").fetchone()[0])
        return
    if args.admin_delete:
        with db_connect() as con:
            n = con.execute("DELETE FROM saved WHERE code = ?", (args.admin_delete,)).rowcount
        print("削除しました" if n else "その ID は見つかりません")
        return

    purge_old()
    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        raise SystemExit("起動できませんでした (%s)。すでに別の server.py が動いていないか、ポート %d が使われていないか確認してください。" % (e, args.port))
    print("保存サーバーを起動しました:  http://%s:%d/   (データ: %s)   止めるには Ctrl+C" % (args.host, args.port, CONFIG["db"]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n止めました")


if __name__ == "__main__":
    main()
