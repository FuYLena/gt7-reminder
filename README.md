# GT7 觀賽領車提醒

GitHub Actions 每天柏斯時間 07:00、19:00 執行，到了提醒日就推播 Telegram。

**報名提醒**：直播開始當天（附截止時間）、截止前 5 天、截止當天。按「✅ 已報名」後就不再提醒報名。

**領取提醒**：只對按過「✅ 已報名」的那一站發送。獎勵送到車庫當天（附領取截止時間），以及截止前一天各一次。按「✅ 已領」後就不再提醒。

## 檔案
- `campaigns.json`：各站日期。Claude 的雲端排程任務每週查官網後更新，也可以手動編輯。
- `state.json`：已報名、已領、已發送的紀錄，由 Actions 自動更新。
- `remind.py`：提醒邏輯。

## Secrets
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 手動測試
Actions → GT7 reminders → Run workflow。
