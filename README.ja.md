<!-- mcp-name: io.github.shigechika/boxadm-mcp -->

# boxadm-mcp

[English](README.md) | 日本語

Box の管理（admin）視点で**組織外への情報フローを可視化**する MCP
（Model Context Protocol）サーバ。Box の enterprise event ログ（`admin_logs`）
を読み取り、「外部とのファイルやり取りが多い人」「外部からアクセスの多い
ファイル」を炙り出す — 情報漏洩の予兆に気づくための早期警戒であり、
汎用のファイルブラウザではない。

**read-only**： 共有解除・削除などの変更操作は一切行わない — リスクを
見せるだけ。汎用の Box ファイル操作 MCP（公式 Box MCP・claude.ai の Box
コネクタ）とは別物で、あちらはユーザー自身のファイルを扱い enterprise
events は見られない — それこそがこのサーバの存在意義。

管理コンソールの視点にちなんで `boxadm`（= Box admin）と命名。
[`gwsadm-mcp`](https://github.com/shigechika/gwsadm-mcp) の姉妹サーバ。

## 機能

| ツール | 種別 | 説明 |
|------|------|------|
| `health_check` | — | version + auth_mode + Box 認証 + `admin_logs` スコープ疎通 + 設定済み内部ドメイン allowlist。未ログイン（OAuth モード）は `needs-login` を返す |
| `recent_admin_events` | 診断 | 直近の enterprise events を生で返す（イベント種別・フィールド確認用）。`stream_position` で手動ページ送り可 |
| `external_access_events` | アクセス（events・全社横断） | 窓内の外部 DOWNLOAD/PREVIEW を集計。外部アクセス元 top・外部被アクセスファイル top・リンク経由数。`created_by_logins` 指定で特定アカウントの**逆引き（DLP 追跡）** |
| `external_collaborators` | 露出（列挙） | 外部 collaborator（組織外 login / 外部招待メール）を列挙 |
| `public_shared_links` | 露出（列挙） | `open`（誰でもリンク）共有の item を列挙 |
| `top_external_sharers` | 露出（列挙） | 内部 owner を外部露出（外部 collab + 公開リンク）で順位付け |
| `daily_brief` | 統合 | アクセス（events）× 露出（列挙）の朝サマリ |

## 認証方式

`BOX_AUTH_MODE` で2モードから選択:

- `oauth` — OAuth 2.0（ユーザー認証）。管理者がブラウザで一度認可すれば、
  以降は refresh token で無人稼働する。
- `ccg` — Client Credentials Grant（サーバ間）。テナントに
  サーバー認証アプリの空き枠があれば、こちらの方が無人運用は単純。

`admin_logs`（enterprise events）は**どちらのモードでも**読める。条件は
「認可/委任されたユーザーが管理者」＋「アプリに **Manage enterprise
properties** スコープ」があること。

### OAuth セットアップ（Box 管理者が一度だけ）

1. Developer Console → Create Platform App → **Custom App → User
   Authentication (OAuth 2.0)**
2. **リダイレクト URI**： `http://localhost:8787/callback`
3. **Application Scopes**： **Manage enterprise properties** にチェック
   （`admin_logs` に必須）。collaboration/共有リンクの列挙も使うなら
   **Read all files and folders** も追加（再同意が必要）
4. Admin Console でアプリを有効化する（多くのテナントポリシーでは未公開
   アプリは既定無効）
5. **Client ID / Client Secret** を控える
6. 初回ログイン： `BOX_AUTH_MODE=oauth` 等を設定して **`boxadm-mcp auth`**
   を実行 → ブラウザで認可 → token cache（`~/.config/boxadm-mcp/token.json`、
   chmod 600）が作成される

## セットアップ

```bash
# uv
uv pip install boxadm-mcp

# pip
pip install boxadm-mcp
```

またはソースから:

```bash
git clone https://github.com/shigechika/boxadm-mcp.git
cd boxadm-mcp

# uv
uv sync

# pip
pip install -e .
```

## 設定

| 変数 | 必須 | 説明 |
|---|---|---|
| `BOX_AUTH_MODE` | | `oauth` / `ccg`（既定 `ccg`） |
| `BOX_CLIENT_ID` | ✓ | アプリの Client ID |
| `BOX_CLIENT_SECRET` | ✓ | アプリの Client Secret |
| `BOX_ENTERPRISE_ID` | ccg 時 | Enterprise ID（CCG の subject。oauth では不要） |
| `BOX_OAUTH_REDIRECT_URI` | | oauth の redirect。既定 `http://localhost:8787/callback` |
| `BOX_TOKEN_CACHE` | | oauth の token cache パス。既定 `~/.config/boxadm-mcp/token.json` |
| `BOX_API_BASE` | | 既定 `https://api.box.com` |
| `BOX_SCAN_CONCURRENCY` | | 列挙スキャンのフォルダ単位並列数。既定 `8`、範囲 `1`–`32` にクランプ |
| `BOX_ALLOWED_DOMAINS` | ✓ | 内部メールドメイン（カンマ区切り）。既定値なし — 設定するまで全アドレスが外部扱いになる |

secret は `.mcp.json` に直書きせず（例: 起動前に読み込むローカル env
ファイルに置く）、`.mcp.json` 自体は `${BOX_CLIENT_ID}` のような変数参照
のみにすれば安全にコミットできる。

### 射程と上限

- **アクセス系**（`external_access_events`、および `daily_brief` の
  access 部分）は **events ストリーム＝全社横断**を読む。`max_events`
  上限に達すると `capped: true`（古い順スキャン）。
- **露出系（列挙）**は **co-admin アカウントから見えるフォルダ範囲**
  （全社 100% を保証しない）＋ `max_folders`/`max_depth` 上限（`capped`
  で開示）。**Read all files and folders** スコープが必要。
- スキャンはフォルダ単位の照会を並列に投げる（`BOX_SCAN_CONCURRENCY`）。
  Box には全社横断の collaboration 一括取得 API が無いためで、tool 呼び出し
  タイムアウト内に走査できるフォルダ数を広げる（ただし上限による制限は残る）。
  フォルダ単位の API エラー（403 や一時的な 429 など）で落ちたフォルダは
  `fetch_errors` に計上される。網羅と言えるのは `capped` が false **かつ**
  `fetch_errors` が 0 のときだけ。
- 列挙系ツールは短 TTL のスキャンメモを呼び出し間で共有する。
  `public_shared_links` は collaboration 呼び出しを一切行わない
  （最適化）。

### DLP 追跡（accessor からの逆引き）

「この外部アカウントが何をダウンロードしたか」を特定する用途。
`external_access_events` に `created_by_logins`（カンマ区切りの login）
を渡すと、その accessor のイベントだけを残し、ファイル明細
（`matched_events`： item id/name・owner・サイズ（bytes+GB）・日時・
event_type・共有リンク経由か）を返す。

```
external_access_events(since_hours=26, created_by_logins="someone@example.com")
```

- accessor が窓内のどこに現れるか不明なため、フィルタ指定時はスキャン
  上限を**最大 50,000 events** まで自動拡張（古い順スキャン）— ただし
  keep するのは一致イベントのみなのでメモリは有界。
- このモードでは戻り値が `events_scanned` ではなく **`events_matched`
  （一致件数）** になる（走査総数は保持しない。網羅性は `capped` で
  判定する）。`capped: true` は窓を走査し切れていない合図（`max_events`
  を上げる）。
- Box の `admin_logs` API には `created_by` クエリパラメータが無いため、
  クライアント側フィルタで実現している（`fetch_admin_events
  (created_by_logins=...)`）。

## 使い方

### Claude Code

`.mcp.json` に追加する:

```json
{
  "mcpServers": {
    "boxadm-mcp": {
      "type": "stdio",
      "command": "boxadm-mcp",
      "env": {
        "BOX_AUTH_MODE": "oauth",
        "BOX_CLIENT_ID": "${BOX_CLIENT_ID}",
        "BOX_CLIENT_SECRET": "${BOX_CLIENT_SECRET}",
        "BOX_ALLOWED_DOMAINS": "example.com"
      }
    }
  }
}
```

### CLI オプション

```bash
boxadm-mcp auth       # OAuth 初回ログイン（ブラウザが開く）
boxadm-mcp --version  # バージョンを表示して終了
boxadm-mcp            # MCP サーバを起動（STDIO、既定）
```

## 開発

```bash
git clone https://github.com/shigechika/boxadm-mcp.git
cd boxadm-mcp

# uv
uv sync --dev
uv run pytest -v
uv run ruff check .

# pip
python3 -m venv .venv
.venv/bin/pip install -e . && .venv/bin/pip install pytest respx ruff
.venv/bin/pytest -v
.venv/bin/ruff check .
```

テストは Box に一切接続しない — `respx` が CCG/OAuth のトークンエンドポイント
と `admin_logs`/列挙系 API をモックする。

## リリース

リリースは [release-please](https://github.com/googleapis/release-please) で
自動化されている。[Conventional Commits](https://www.conventionalcommits.org/)
（`feat:`、`fix:` 等）を `main` にマージすると、次バージョンと changelog を
持つリリース PR が維持される。その PR をマージすると `vX.Y.Z` がタグ付けされ
GitHub Release が公開され、`release: published` イベントが `release`
workflow を起動して PyPI と MCP Registry へビルド・公開する。バージョンは
`boxadm_mcp/__init__.py` と `server.json` の両方を release-please が管理する
（手動で書き換えないこと）。

> [!IMPORTANT]
> release-please の workflow にはリポジトリシークレット `RELEASE_PLEASE_TOKEN`
> （`contents: write` + `pull-requests: write` を持つ PAT）を設定すること。
> 既定の `GITHUB_TOKEN` は下流の `release` workflow を起動する Release を
> 作成できない（GitHub が `GITHUB_TOKEN` 起因の workflow 起動をブロックする
> ため）ので、PAT がないと何も公開されない。シークレット未設定時は
> `GITHUB_TOKEN` にフォールバックするので、fork 上でも PR CI は動作する。

## ガバナンス

利用者のファイル共有状況を見る性質上、**認可された情報セキュリティ監視**
として目的・閲覧者・保持期間を明確にして運用すること。外部共有自体は
正当なもの（共同研究先・業者とのやり取り）も多いため、アラートではなく
**リスクのランキング表示**として扱い、既知 OK な共有先は除外リストとして
育てていく。

## ライセンス

MIT
