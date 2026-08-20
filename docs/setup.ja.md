# セットアップ

## インストール

```bash
uv pip install boxadm-mcp
# または
pip install boxadm-mcp
```

ソースから:

```bash
git clone https://github.com/shigechika/boxadm-mcp.git
cd boxadm-mcp
uv sync          # または: pip install -e .
```

## 認証方式

`BOX_AUTH_MODE` で2モードから選択します:

- `ccg`（既定） — Client Credentials Grant（サーバ間）。テナントにサーバー
  認証アプリの空き枠があれば、こちらの方が無人運用は単純です。
- `oauth` — OAuth 2.0（ユーザー認証）。管理者がブラウザで一度認可すれば、
  以降は refresh token で無人稼働します。

`admin_logs`（enterprise events）は**どちらのモードでも**読めます。条件は
「認可/委任されたユーザーが管理者」＋「アプリに **Manage enterprise
properties** スコープ」があることです。

### OAuth セットアップ（Box 管理者が一度だけ）

1. Developer Console → Create Platform App → **Custom App → User
   Authentication (OAuth 2.0)**
2. **リダイレクト URI**: `http://localhost:8787/callback`
3. **Application Scopes**: **Manage enterprise properties** にチェック
   （`admin_logs` に必須）。collaboration/共有リンクの列挙も使うなら
   **Read all files and folders**、`get_user` の照会も使うなら
   **Manage users（ユーザーを管理する）** も追加（スコープ変更のたびに
   `boxadm-mcp auth` での再同意が必要）
4. Admin Console でアプリを有効化する（多くのテナントポリシーでは未公開
   アプリは既定無効）
5. **Client ID / Client Secret** を控える
6. 初回ログイン: `BOX_AUTH_MODE=oauth` 等を設定して `boxadm-mcp auth` を
   実行 → ブラウザで認可 → token cache（`~/.config/boxadm-mcp/token.json`、
   chmod 600）が作成される

!!! tip "ccg モードはブラウザもローカルの token cache も不要"
    テナントにサーバー認証アプリの空き枠があれば、`ccg` モードは平文の
    文字列シークレット（`BOX_CLIENT_ID`・`BOX_CLIENT_SECRET`・
    `BOX_ENTERPRISE_ID`）だけを読み、`BOX_TOKEN_CACHE` には一切触れません
    — マシンごとに用意するものがありません。

## 環境変数

| 変数 | 説明 | 既定 |
|---|---|---|
| `BOX_AUTH_MODE` | `oauth` / `ccg`。それ以外の値は `ccg` にフォールバック。`health_check` が実際のモードを返す | `ccg` |
| `BOX_CLIENT_ID` | アプリの Client ID | *必須* |
| `BOX_CLIENT_SECRET` | アプリの Client Secret | *必須* |
| `BOX_ENTERPRISE_ID` | Enterprise ID（CCG の subject。oauth では不要） | *ccg モードでは必須* |
| `BOX_OAUTH_REDIRECT_URI` | oauth の redirect | `http://localhost:8787/callback` |
| `BOX_TOKEN_CACHE` | oauth の token cache パス（ファイルパス。oauth モードでのみ使われ、`boxadm-mcp auth` が自己生成する） | `~/.config/boxadm-mcp/token.json` |
| `BOX_API_BASE` | Box API のベース URL | `https://api.box.com` |
| `BOX_SCAN_CONCURRENCY` | 列挙スキャンのフォルダ単位並列数 | `8`（範囲 1–32 にクランプ） |
| `BOX_SCAN_DEADLINE` | 列挙スキャン1回のソフトな実時間バジェット（秒）。`0`/負値で無効化。到達すると部分結果（`capped=true`）を開示して返す | `45` |
| `BOX_HTTP_TIMEOUT` | リクエスト単位の HTTP タイムアウト（秒） | `30` |
| `BOX_ALLOWED_DOMAINS` | 内部メールドメイン（カンマ区切り）。既定値なし — 設定するまで全アドレスが外部扱い | *必須* |

secret は `.mcp.json` に直書きせず（例: 起動前に読み込むローカル env
ファイルに置く）、`.mcp.json` 自体は `${BOX_CLIENT_ID}` のような変数参照
のみにすれば安全にコミットできます。

## MCP クライアントへの登録

### Claude Code（プラグイン）

このリポジトリはプラグイン 1 個のマーケットプレイスも兼ねています。

```
/plugin marketplace add shigechika/boxadm-mcp
/plugin install boxadm-mcp@boxadm-mcp
```

プラグインは `uvx boxadm-mcp` を起動し、上記の環境変数と同じものを
読みます。Claude Code を起動する前に `BOX_CLIENT_ID`・`BOX_CLIENT_SECRET`・
`BOX_ENTERPRISE_ID`・`BOX_ALLOWED_DOMAINS` を export しておいてください。
プラグインは既定で `BOX_AUTH_MODE=ccg` を使います — `oauth` に切り替えるのは
自分で一度 `boxadm-mcp auth` を実行した後にしてください。ブラウザでの認可手順や
生成される token cache ファイルはプラグイン側では用意できません。

プラグインは `uvx` を起動するため、Claude Code を実行するプロセスの `PATH` に
`uvx` が通っている必要があります。ログインシェルなら通常問題ありませんが、
GUI から起動した場合は通っていないことがあります。プラグインが起動しない場合は
[uv](https://docs.astral.sh/uv/) をシステム全体にインストールしてください。

### Claude Code（手動）

`.mcp.json`:

```json
{
  "mcpServers": {
    "boxadm-mcp": {
      "type": "stdio",
      "command": "boxadm-mcp",
      "env": {
        "BOX_AUTH_MODE": "oauth",
        "BOX_CLIENT_ID": "${BOX_CLIENT_ID:-}",
        "BOX_CLIENT_SECRET": "${BOX_CLIENT_SECRET:-}",
        "BOX_ALLOWED_DOMAINS": "example.com"
      }
    }
  }
}
```

### CLI

```bash
boxadm-mcp auth       # OAuth 初回ログイン（ブラウザが開く）
boxadm-mcp --version  # バージョンを表示して終了
boxadm-mcp            # MCP サーバを起動（STDIO、既定）
```

引数無しモードが通常の使い方です。MCP クライアントはこの形で起動します。

## 次に

[リファレンス](reference.ja.md) で全ツール・射程と上限・DLP 追跡・
`get_user` の完全一致ルールを扱います。
