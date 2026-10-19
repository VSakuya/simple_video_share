# Simple Video Share

[English](./README.md) | [日本語](./README.ja.md) | [简体中文](./README.zh-CN.md)


**非常に低スペックなサーバー**向けに設計された軽量な動画共有 Web アプリです。
重い処理（トランスコード、クリップ編集、カバー抽出）はすべて
**ブラウザ側クライアント**で実行されます（WebCodecs + mediabunny、必要時は
ffmpeg.wasm にフォールバック）。サーバー側はファイルを一時キャッシュ
（LRU 退避、空き容量 500 MB を確保）するだけで、バックグラウンドワーカーが
**Google Drive** へミラーリングします。

## 機能

- マルチユーザーアカウント（管理者作成のみ、自己登録なし）。アバター対応、
  初回ログイン時のパスワード変更を必須化。
- アップロードキューでクライアント側トランスコードを実施（AV1/H.264、
  上限 1080p / 60 fps / 管理者設定ビットレート）。QuickSplit 風の任意クリップ
  エディタと、1 GB 超ファイルの分割アップロードに対応。
- 視聴ページは HTTP Range ストリーミング、Google Drive からの必要時キャッシュ
  ダウンロード、コメント（二段階返信、顔文字歓迎）、タグ、ダウンロードボタンを
  提供。
- フォルダ（無制限のネスト階層）とタグ管理システム。
- ライブルーム（Apache 配下の Node-Media-Server FLV 配信、配信中検知、
  カバー/タイトル管理、在室情報 + 永続チャット）。
- Watch Together ルーム（同期再生、在室表示、揮発性チャット）。
- 管理コンソール：動画 / ユーザー / ライブルーム / タグ / 設定 / サーバー &
  クライアントログ、さらにチェックサム検証付きのストレージ import/export。

## 要件

- Python **3.12** 以上（仮想環境は `.venv/` を使用）。
- `pip install -r requirements.txt`（flask, pydrive2, waitress）。
- プロジェクトルートに `config.json` を配置（`config.example.json` をコピー）。
  secret key は初回起動時に自動生成。
- `credentials/` 配下に Google Drive OAuth 認証情報（実行時にアプリが管理。
  手動編集しないこと）。
- 本番環境は同梱の Apache vhost `000-default.conf` を使用
  （HTTPS、`127.0.0.1:8080` の Waitress へのリバースプロキシ、FLV 再生保護）。

## 実行方法

- ローカル開発: `python main.py`
