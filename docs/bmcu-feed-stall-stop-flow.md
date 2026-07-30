# BMCUフィード停止時の造形停止フロー要件

## 1. 目的と用語

BMCUの押出ギアが空転・停止し、プリンタがエラーを出さずに空中造形を継続する事故を、BambuddyがBMCU Linkテレメトリから検出して止める。

本書では操作を次の2段階に分ける。

- **自動一時停止（PAUSE）**: 復旧・再開可能。現行実装の自動保護動作。
- **強制終了（STOP/CANCEL）**: 造形を不可逆に終了。Bambuddyには送信APIが存在するが、フィード停止からの自動実行は未実装。

誤検知で正常品を廃棄しないため、既定動作はPAUSEとする。STOPへの自動昇格を追加する場合は、明示的な設定と独立した確認フローを必須とする。

## 2. システム境界

停止判断とプリンタ操作はBambuddyが所有する。

1. BMCUが管理UARTへSTATUS/EVENTを出力する。
2. Picoが受信・CRC検証・重要度分類・再送を行い、Bambuddyへpushする。
3. Bambuddyが最新STATUSとMQTT由来プリンタ状態を組み合わせて異常を判定する。
4. BambuddyがプリンタへMQTT QoS 1でPAUSEを送る。
5. BambuddyがMQTT状態の`PAUSE`遷移を確認して初めて成功扱いする。

Picoは停止コマンドを発行しない。Pico/Bambuddyの通信断だけを根拠にプリンタを止めない。

## 3. 現行の検出条件

Bambuddyは2秒周期で、設定対象プリンタとリンクを監視する。次の条件がすべて連続して成立した場合だけフィード停止とする。

- プリンタが接続済みかつ`RUNNING`
- `BAMBUDDY_FEED_STALL_LINK_ID`で選択したBMCU STATUSが20秒以内の新鮮なデータ
- `current_slot`、`pull_pct[]`、`motion[]`が妥当
- 現在スロットの`motion == 2`（on-use）
- `pull_pct[current_slot] <= 5`
- 上記が30秒継続

早期警告は、on-use中に`pull_pct < 20`が5秒継続するとDiscordへ通知する。この段階ではプリンタ操作を行わない。

欠損、不正形式、stale、非RUNNING、スロット変更、on-use解除、pull回復はエピソードを解除する。データ欠損を異常の証拠にはしない。

### F06Fの位置づけ

Picoは`state_change(field=5, value=0xF06F)`を即時・criticalイベントとして保持し、通常の圧力揺動より優先してBambuddyへ送る。ただし現行BambuddyはF06F単独で停止せず、上記STATUS複合条件を停止判断の正本とする。F06F直結停止を追加する場合も、fresh STATUSとRUNNINGを併用して誤検知を防ぐこと。

## 4. 実装済みの確認付きPAUSE

フィード停止トリガー後、Bambuddyは次の閉ループを実行する。

1. `client.pause_print()`で`{"print":{"command":"pause","sequence_id":"0"}}`をMQTT QoS 1送信する。
2. 0.5秒間隔で最大6回、BambuddyのMQTT由来プリンタ状態を読む。
3. `PAUSE`を観測した場合だけ成功とする。
4. `RUNNING`のままならPAUSE送信を最大3回繰り返す。
5. `FINISH`、`FAILED`、`IDLE`などRUNNING系以外へ遷移した場合は再送を止め、未確認結果として扱う。
6. PAUSE未確認ならエピソードをラッチせず、次の2秒監視周期でも再試行する。
7. Discord通知には「自動PAUSE成功」または「送信したがPAUSE未確認」を明記する。

`pause_print()`の戻り値はMQTT publishを試みたことしか示さない。戻り値`True`だけで成功扱いしてはならない。

## 5. Bambuddy側の必須対応

- 最新STATUSキャッシュをDB flush後も保持し、監視周期ごとに欠損扱いしない。
- 最新STATUSを`(device_id, link_id)`単位で管理する。
- 監視対象リンクを`BAMBUDDY_FEED_STALL_LINK_ID`で固定できること。
- WebSocket ingestのACK経路でSQLite flushを待たないこと。
- ACKの`persisted` watermarkはDB commit後のデータだけを示すこと。
- PAUSE publishと状態確認を分離し、確認付き再試行を行うこと。
- stale/offline通知を回復安定期間なしに再armしないこと。
- 検出、送信回数、最終プリンタ状態、対象リンク、スロット、層番号をログへ残すこと。

現行作業ブランチではコミット`f0f875cf`と`6e6e64be`がこれらを実装している。実運用にはBambuddyのデプロイが必要であり、Picoだけ更新しても自動PAUSEは有効にならない。

## 6. 自動STOPへ昇格する場合の追加要件

自動STOPは未実装。追加する場合は次を満たすこと。

- 既定OFFの明示設定（例: `BAMBUDDY_FEED_STALL_ESCALATE_STOP=0`）を設ける。
- まず確認付きPAUSEを実行し、一定猶予後もfreshなMQTT状態が`RUNNING`の場合だけSTOP候補とする。
- STOP直前にも、同じ印刷ジョブID、接続状態、fresh BMCU異常が継続していることを再確認する。
- `client.stop_print()`のpublish戻り値だけで成功扱いせず、`RUNNING/PAUSE`から終了状態への遷移を確認する。
- STOPは1エピソード・1ジョブにつき一度だけ発行し、再送する場合も回数上限を設ける。
- 自動STOPとユーザー操作のSTOPを監査ログ・アーカイブ上で区別する。
- DiscordへSTOP発行前、発行後、未確認を別々に通知する。
- stale、Bambuddy再起動直後、プリンタ再接続直後、フィラメント交換中にはSTOPへ昇格しない。

STOPは造形を救済できなくするため、まず実機でPAUSE確認フローを検証し、その結果を見てから有効化する。

## 7. 受け入れ条件

- 正常造形中の圧力揺動でPAUSE/STOPしない。
- 指でフィラメント供給を妨げた再現試験で、複合条件成立後にPAUSE要求が出る。
- MQTT publish成功でも状態がRUNNINGのままなら成功表示しない。
- PAUSE確認後は同一エピソードで再送しない。
- PAUSE未確認時は再試行し、通知とログに最終状態を残す。
- BMCU Link切断・staleのみでは停止しない。
- 2台接続時も監視対象リンクを取り違えず、片側のイベント洪水で対象STATUSを失わない。
- Bambuddy再起動、Pico再起動、ACK timeout、DB flush遅延後も同じ安全条件を維持する。
