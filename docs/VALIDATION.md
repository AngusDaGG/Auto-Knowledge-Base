# 初版驗收紀錄

驗收日期：2026-10-23。Windows、Python 3.12、Node 24；模型與第三方 runtime 由本機既有安裝提供，沒有包入 repository。

## 真實完成

- 新入口在獨立工作區呼叫移植後引擎，完成一支 577 秒公開 YouTube 影片的實際取得與本機 Whisper 辨識，產生取得收據與 97 段 ASR。
- Agent 讀取完整 ASR，完成有方法、條件、限制、來源時間戳與主張標籤的影片知識卡；影片末端 ASR 至 560.49 秒，未將剩餘區段宣稱為已辨識語音。
- 另外建立一份合成 Markdown，完成掃描、實際 Agent 精煉、文件卡驗證與 Windows 原件歸檔；原件、快照、歸檔 bytes 的 SHA-256 一致。
- 唯一主題索引對兩卡各列一次。從另一工作目錄以絕對 CLI 路徑搜尋兩個別名，分別命中正確來源卡。
- 再貼同一影片後，取得收據 hash、mtime、ASR hash 及結果紀錄行數不變：沿用已有內容，沒有重複下載或辨識。

實際影片來源、逐字稿、卡片、使用者路徑與詳細 runtime 設定保留於驗收者本機，未放入本公開程式庫。

## 離線驗證

測試覆蓋平台分流、bounded retry／拒絕邊界、來源與片長收據、同片多網址、文件內容去重、續跑、schema、索引、人工修改、秘密遮罩、API 拒答／截斷、localhost HTTP、來源引文與時間戳、索引鎖定及 I/O 回復。

發布測試使用真正的本機 bare Git repo，包含程式變更與其他本機提交不被帶入、完整 outgoing commit allowlist、遠端前進／衝突、private target 綁定及遠端內容讀回。這是離線 Git 整合，並非真實 GitHub 知識發布。

## 證據邊界

- 初版 API 精煉以 mock／localhost 驗證；未宣稱任一真實雲端或本機 LLM 已通過端到端驗收，未花費外部模型 API 額度。
- 新版沒有重新對每個平台跑實際影片；繼承的引擎回歸測試不等於所有平台今天都可取得。平台拒絕仍會停止。
- 真實驗收的私人知識發布設定為關閉。開源程式發布到 GitHub，不代表使用者的私人知識已發布。
- Windows 索引寫入具原生鎖、寫前備份與正常 I/O 回復；不保證任意斷電原子性。異常中斷時核對 `state/card-candidates/` 的 `index-before.md`、`index-after.md`、`index-transaction.json`，不要覆寫後續人工變更。
- 未做 Linux／macOS 完整驗收。機械欄位檢查、來源引句、CI 或測試數量都不保證作者主張真實或語意完整。

最新 GitHub CI 與 commit 可在 repository Actions／PR 頁查看；只以實際通過的執行版本為準。
