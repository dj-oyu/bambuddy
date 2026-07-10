# CLAUDE.md

bambuddy 私家版フォーク(dj-oyu/bambuddy)の開発ガイド。上流は maziggy/bambuddy。
Python 3.11 / FastAPI / SQLAlchemy(async) / SQLite のバックエンドと React フロントエンド。
Bambu Lab A1 mini + BMCU(サードパーティAMS)+ Chitu C1M プレートチェンジャーでの連続印刷運用が主目的。

## リポジトリと運用の全体像

- **この checkout(/app/bambuddy)は開発用。実働サービスは /opt/bambuddy(bambuddyユーザー所有)で systemd 稼働。**
  ここを編集してもサービスには反映されない。反映は `./deploy.sh`(rsync + restart + ヘルスチェック)か、
  変更ファイルだけ `sudo cp` + `sudo systemctl restart bambuddy`。
- リモート: `origin` = dj-oyu/bambuddy(push先)、`upstream` = maziggy/bambuddy(fetch専用、push無効)。
- ブランチ: 私家版パッチはすべて `local` に積む。上流取り込みは
  `git fetch upstream main:main && git merge main`(local上で)→ テスト → push → deploy。
- 実データ: DB は `/mnt/petcam-data/bambuddy/data/bambuddy.db`(systemd drop-in の DATA_DIR)。
  `/opt/bambuddy/data` の DB は使われていない残骸。設定系 drop-in は
  `/etc/systemd/system/bambuddy.service.d/`(HMS自動クリアコード等の環境変数もここ)。

## 開発方針

- **稼働中のプリンタがいる前提で動く。** bambuddy の再起動は印刷に影響しない(印刷は機体側で走る)が、
  DB の print_queue を直接いじるときはスケジューラの30秒ティックとの競合を考えること
  (pending に戻す順番・タイミングで即ディスパッチされる)。
- 私家版パッチには理由をコミットメッセージに書く(症状→原因→設計判断)。壊れ方は常に安全側
  (fail-safe: 何もしない=従来動作)に倒す。例: deferred-unload はパターン不一致なら除去しない。
- 環境変数トグルを付ける(例: `BAMBUDDY_DEFER_TAIL_UNLOAD=0`)。上流マージで壊れても機能単位で切れるように。
- テスト: `/opt/bambuddy/venv/bin/python3.11 -m pytest backend/tests/unit/ -q`(ホストのpython3には依存が無い)。
  新パッチには必ずユニットテストを付け、実機のスライサー出力をフィクスチャに使う。
- G-code 加工は必ず `.gcode.md5` サイドカーの再計算を通す(P1S系ファームが検証する)。
- ログでの動作確認が第一次情報源: `journalctl -u bambuddy`。printer状態は
  `curl localhost:8000/api/v1/printers/1/status`(認証は現在無効)。

## エージェントの使い分け

- **Explore**: 「どこで何をしているか」を横断的に探すとき(複数ファイル・命名規則をまたぐ調査)。
  読み取り専用。結論だけ欲しい探索はこれに投げ、自分でファイルダンプを抱え込まない。
- **Plan**: 実装前の設計検討。複数サブシステム(scheduler / bambu_mqtt / threemf_tools / frontend)を
  またぐ変更や、上流マージのコンフリクト解消方針はまず Plan に設計させる。
- **general-purpose**: 検索の当たりが数回で付かなそうな調査、独立した複数ステップ作業の並行実行。
- **codex:codex-rescue**: 行き詰まったとき・第二の診断視点が欲しいとき。自前の結論に確信が持てない
  根本原因調査で積極的に使う。
- **レビューは別モデル/別コンテキストで**: 私家版パッチをデプロイする前に、広いコンテキストを持つ
  エージェント(model: opus 等)に「影響範囲」と「コード品質」を分けてレビューさせる。
  レビュー指摘は severity 付きで報告させ、修正後に対象テストを全部回す。

## オーケストレーションのルール

- **調査→設計→実装→レビューの各フェーズを分け、フェーズ間は自分(メインループ)が判断する。**
  一括で丸投げしない。各エージェントの結論だけ受け取り、次のフェーズの入力に要約して渡す。
- 独立な調査は**同一メッセージで並列に**投げる(直列に待たない)。依存があるものだけ順に。
- 実ファイルを並行で変更させる場合は worktree 隔離(`isolation: worktree`)を使う。
  ただしこのリポジトリは /opt との二重管理なので、デプロイは必ずメインループが一元的に行う。
- 長時間の見張り(印刷状態・ログ監視)は Bash の `run_in_background` + until ループで。
  ポーリングの sleep 連打はしない。
- サブエージェントには「返答=生データ」で返させ、ユーザー向けの整形はメインループがやる。
  日本語ユーザーなので最終報告は日本語、コード・コミットは英語。
- Workflow(多段ファンアウト)はユーザーが明示的に求めたときだけ。普段の規模なら Agent 数個で足りる。

## 環境の注意

- sudo は使える。credential(APIキー等)は部分的にも echo しない(権限クラシファイアに弾かれる)。
- 一時ファイルはセッションの scratchpad へ。/tmp 直書きしない。
- タイムゾーン: サービスは TZ=Asia/Shanghai(CST)。DB のタイムスタンプは UTC。ログは CST。混同注意。
