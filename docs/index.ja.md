# boxadm-mcp

Box の管理（admin）視点で**組織外への情報フローを可視化**する MCP
サーバーです。Box の enterprise event ログ（`admin_logs`）を読み取り、
「外部とのファイルやり取りが多い人」「外部からアクセスの多いファイル」を
炙り出します — 情報漏洩の予兆に気づくための早期警戒であり、汎用のファイル
ブラウザではありません。

**read-only**: 共有解除・削除などの変更操作は一切行わず、リスクを見せる
だけです。汎用の Box ファイル操作 MCP（公式 Box MCP・claude.ai の Box
コネクタ）とは別物で、あちらはユーザー自身のファイルを扱い enterprise
events は見られません — それこそがこのサーバーの存在意義です。

管理コンソールの視点にちなんで `boxadm`（= Box admin）と命名しました。
[`gwsadm-mcp`](https://github.com/shigechika/gwsadm-mcp) の姉妹サーバーです。

## 領域別ツール

| 領域 | ツール | 説明 |
|---|---|---|
| — | `health_check` | version + auth_mode + Box 認証 + `admin_logs` スコープ疎通 + 設定済み内部ドメイン allowlist |
| 診断 | `recent_admin_events` | 直近の enterprise events を生で返す（イベント種別・フィールド確認用） |
| アクセス（events・全社横断） | `external_access_events` | 窓内の外部 DOWNLOAD/PREVIEW を集計。accessor 単位の DLP 追跡にも対応 |
| 露出（列挙） | `external_collaborators` | 外部 collaborator を列挙 |
| 露出（列挙） | `public_shared_links` | 誰でもリンクの共有 item を列挙 |
| 露出（列挙） | `top_external_sharers` | 内部 owner を外部露出で順位付け |
| フォルダ1つの一覧（`ls`） | `list_folder_items` | 1フォルダ内の各アイテムの名前・アップロード日時・サイズ・アップロード者・直リンク |
| アカウント状態（単体照会） | `get_user` | login 完全一致で1アカウントを照会：status・role・enterprise・容量・日時 |
| 統合 | `daily_brief` | アクセス（events）× 露出（列挙）の朝サマリ |

**書き込みを行うツールはありません。** ここにあるツールはすべて Box の
`admin_logs` とディレクトリ API を読み取るだけなので、書き込み権限で
ゲートすべき対象自体が存在しません。

## 設計方針

**2つの認証モード、1つの読み取り範囲。** `BOX_AUTH_MODE` で `ccg`
（Client Credentials Grant、サーバー間、既定）と `oauth`（ブラウザでの
一度きりのユーザー認可）を選べます。`admin_logs` はどちらのモードでも
読めます。条件は「認可/委任されたユーザーが管理者」＋「アプリに
**Manage enterprise properties** スコープ」があることです。

**取得を打ち切ったときは、打ち切ったことを明示します。** `BOX_SCAN_DEADLINE`
が列挙スキャンの実行時間を、`BOX_SCAN_CONCURRENCY` がフォルダ単位の並列数を
制限します。スキャンが途中で打ち切られた場合、応答は `capped: true` を
セットし、たまたま取得できた分をそのまま完全な結果として返すことは
ありません。完全に見える部分的な結果は、遅い結果よりも悪いものです。

**完全一致の照会は完全一致のままにします。** `get_user` は login を完全一致・
大文字小文字無視で照合します。裏で使う Box の `filter_term` は前方一致検索
なので、それ以上の一致は識別情報を返さず件数のみの `other_prefix_hits` に
数え、他人のアカウントを一致として提示しません。

## 次に読むもの

- [セットアップ](setup.ja.md) — 認証設定・環境変数・MCP クライアントへの登録
- [リファレンス](reference.ja.md) — 全ツール・射程と上限・DLP 追跡・`get_user` の照合ルール
