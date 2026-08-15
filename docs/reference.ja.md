# リファレンス

## `health_check()`

稼働中のサーババージョン・実効 `auth_mode`・設定済み Box 認証情報が認証
できるか・`admin_logs` スコープの疎通・設定済み `BOX_ALLOWED_DOMAINS`
allowlist を報告します。`oauth` モードでは初回の `boxadm-mcp auth` 実行前は
エラーで落ちるのではなく `needs-login` を返します。

フォルダを歩いたりイベントストリームをページングしたりしないので、
セッション開始時やツール呼び出しのタイムアウト後に安心して呼べます。

## ツール一覧

| ツール | 種別 | 説明 |
|---|---|---|
| `health_check` | — | version + auth_mode + Box 認証 + `admin_logs` スコープ疎通 + 設定済み内部ドメイン allowlist。未ログイン（OAuth モード）は `needs-login` を返す |
| `recent_admin_events` | 診断 | 直近の enterprise events を生で返す（イベント種別・フィールド確認用）。`stream_position` で手動ページ送り可 |
| `external_access_events` | アクセス（events・全社横断） | 窓内の外部 DOWNLOAD/PREVIEW を集計。外部アクセス元 top・外部被アクセスファイル top・リンク経由数。`created_by_logins` 指定で特定アカウントの**逆引き（DLP 追跡）** |
| `external_collaborators` | 露出（列挙） | 外部 collaborator（組織外 login / 外部招待メール）を列挙 |
| `public_shared_links` | 露出（列挙） | `open`（誰でもリンク）共有の item を列挙 |
| `top_external_sharers` | 露出（列挙） | 内部 owner を外部露出（外部 collab + 公開リンク）で順位付け |
| `list_folder_items` | フォルダ1つの一覧（`ls`） | 各アイテムの名前・アップロード日時・サイズ・**アップロード者**・直リンク。アップロード者や日時範囲で絞り込める。ファイルの中身は読まない |
| `get_user` | アカウント状態（単体照会） | **login 完全一致**で1アカウントを照会。`status`・`role`・`enterprise`・容量・日時 |
| `daily_brief` | 統合 | アクセス（events）× 露出（列挙）の朝サマリ |

**書き込みを行うツールはありません。** ここにあるツールはすべて Box の
`admin_logs` またはディレクトリ API を読み取るだけで、共有解除・削除など
一切の変更操作を行いません。

## 射程と上限

- **アクセス系**（`external_access_events`、および `daily_brief` の access
  部分）は **events ストリーム＝全社横断**を読みます。`max_events` 上限に
  達すると `capped: true`（古い順スキャン）。
- **露出系（列挙）**は **co-admin アカウントから見えるフォルダ範囲**
  （全社 100% を保証しない）＋ `max_folders`/`max_depth` 上限（`capped` で
  開示）。**Read all files and folders** スコープが必要です。
- スキャンはフォルダ単位の照会を並列に投げます（`BOX_SCAN_CONCURRENCY`）。
  Box には全社横断の collaboration 一括取得 API が無いためです。読み取り
  経路は `429`（`Retry-After` を尊重）と一時的な `5xx` をジッター付き
  バックオフでリトライします。そのリトライでも解消しない API エラー（例:
  恒常的な `403`）で落ちたフォルダは `fetch_errors` に計上されます。網羅と
  言えるのは `capped` が false **かつ** `fetch_errors` が 0 のときだけです。
- 列挙系ツールは短 TTL のスキャンメモを呼び出し間で共有します。
  `public_shared_links` は collaboration 呼び出しを一切行いません
  （最適化）。
- **`get_user`** はこれらとは別で、enterprise の**ユーザーディレクトリ**を
  1リクエストだけ読みます。ページングは行わず、構造的に列挙にはなりません。
  検索結果が切り詰められた場合は `capped` で開示し、その場合の
  `found: false` は「存在しない」ではなく「判定不能」を意味します。

## DLP 追跡（accessor からの逆引き）

「この外部アカウントが何をダウンロードしたか」を特定する用途です。
`external_access_events` に `created_by_logins`（カンマ区切りの login）を
渡すと、その accessor のイベントだけを残し、ファイル明細
（`matched_events`: item id/name・owner・サイズ（bytes+GB）・日時・
event_type・共有リンク経由か）を返します。

```
external_access_events(since_hours=26, created_by_logins="someone@example.com")
```

- accessor が窓内のどこに現れるか不明なため、フィルタ指定時はスキャン
  上限を**最大 50,000 events** まで自動拡張します（古い順スキャン）— ただし
  保持するのは一致イベントのみなのでメモリは有界です。
- このモードでは戻り値が `events_scanned` ではなく **`events_matched`
  （一致件数）** になります。網羅性は `capped` で判定します。
  `capped: true` は窓を走査し切れていない合図です（`max_events` を上げる）。
- Box の `admin_logs` API には `created_by` クエリパラメータが無いため、
  クライアント側フィルタで実現しています。

## `list_folder_items`

`ls` であって `cat` ではありません。ファイルの中身は読まず、共有リンクの
作成も一切しません — 既存の共有リンクは「露出している」という所見として
報告するのであって、便利リンクとしてではありません。

**アップロード者は、探しそうな場所には入っていません。** File Request
経由のアップロードでは Box はユーザーを一切記録せず、`created_by` も
`modified_by` も *"Anonymous User"* になり、`owned_by` はアプリのサービス
アカウントになります。投稿者を持っているのは `uploader_display_name` だけ
で、実際の値はメールアドレスです — **不透明な文字列**として完全一致
（大文字小文字は無視）で照合し、メールアドレスとして解釈はしません。
サインイン済みユーザーが普通にアップロードした場合は逆になるので、
`created_by` をフォールバックに使います。

並び順と日時範囲はサーバー側で計算します。`sort=date` は実運用上
`created_at`/`modified_at` の順序と一致しないことがあり、`since`/`until`
は時刻として比較します（両方ともオフセット付きが必須で、日付だけの指定は
拒否します）。

`limit` が縛るのは**返す件数**であって探す範囲ではありません。先に1ページ
丸ごと取得します。`returned` と `matched` の差は呼び出し側の `limit` に
よるもの、`capped` はフォルダが1ページに収まらず「見つからない」が否定
ではなく判定不能であることを指します。

## `get_user`

「このアカウントは無効化されているか、容量は逼迫していないか」に1
リクエストで答えます:

```
get_user(login="someone@example.com")
```

`login` は**完全一致・大文字小文字を無視**して照合します。裏で使う Box の
`filter_term` は表示名と login への前方一致検索なので、`user` に入るのは
login 完全一致のみで、それ以外の hit は `other_prefix_hits` に件数だけ
数え、識別情報は返しません。メールアドレスの形をしていない検索語は
リクエスト前に拒否します。

| フィールド | 意味 |
|---|---|
| `found` | アカウントが存在したかを示す唯一のフィールド。`false` はエラーではなく正常な回答 |
| `user` | `found` のときのアカウント、無ければ `null`: `status`・`role`・`enterprise`・`space_used` / `space_amount`・`created_at`・`modified_at` |
| `other_prefix_hits` | それ以外の前方一致の**件数のみ**。別のアカウントなので識別情報は返さない |
| `capped` | 検索結果が切り詰められた合図。この場合の `found: false` は否定ではなく判定不能 |
| `search_hits`・`note` | 返ってきた件数と、以上を平文で言い直したもの |

このツールでは見つけられないドリフトが1つあります: 同一人物が別ドメインの
2つ目の login を持つケースです。`filter_term` は検索語全体への前方一致
なので、`alice@old.example` で検索して `alice@new.example` が返ることは
ありません。

## CLI

```bash
boxadm-mcp auth       # OAuth 初回ログイン（ブラウザが開く）
boxadm-mcp --version  # バージョンを表示して終了
boxadm-mcp            # MCP サーバを起動（STDIO、既定）
```

引数無しモードが通常の使い方です。MCP クライアントはこの形で起動します。
