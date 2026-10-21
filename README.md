# mBot2 3Dシミュレーター

mBlock と同じブロックを組み立てて、mBot2 の動きを 3D で確認するページです。

- 公開ページ: https://ne23-1258d.github.io/mbot2-3d-sim/ (サーバー保存は使えません)
- 画面: `mbot2-3d-sim.html` (1ファイルで動きます。ライブラリは CDN から読み込みます)
- 保存サーバー: `server.py` (Python 3 の標準ライブラリだけ。データは SQLite)

## 保存サーバーを使う

```
python server.py
```

ブラウザで http://127.0.0.1:8080/ を開くと、画面の「学校のサーバーに保存」で、プログラムの保存・読み込み・削除ができます。
データは `programs.db` に入ります (git には入れません)。

| 設定 | 例 |
|---|---|
| 校内の他のパソコンからも使う | `python server.py --host 0.0.0.0 --port 8080` |
| データの置き場所を変える | `python server.py --db /var/lib/mbot2/programs.db` |

### API
| | |
|---|---|
| `GET /api/programs` | 一覧 |
| `GET /api/programs/<id>` | 1件 (ブロックの XML を含む) |
| `POST /api/programs` | 保存 `{"name":"…","xml":"<xml…","startPose":"line"}`。同じ名前なら上書き |
| `DELETE /api/programs/<id>` | 削除 |

### 注意
- ログイン(認証)はありません。校内のネットワークの中だけで使ってください。インターネットに公開する前に、ログインの仕組みを足す必要があります。
- 別のサイトの画面からの書き込みは断ります (Origin の確認)。1件の大きさは 1MB までです。
- 保存するのはブロックの XML だけです。実機に書き込む機能は、まだありません。

## mBlock に持っていく
画面の「mBlock 用に書き出す (.sb3)」で保存したファイルを、mBlock の「ファイル → コンピューターから開く」で開きます。
