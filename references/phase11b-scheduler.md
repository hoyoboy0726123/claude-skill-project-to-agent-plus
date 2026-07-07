# Phase 11b — 排程提醒 / 主動推送(opt-in)

## 何時加

使用者要 agent **主動**在未來某時間點 / 週期推訊息:
- 「10 分鐘後提醒我洗澡」(單次相對)
- 「每週一到五早上 9 點同步 git」(週期 cron)
- 「明天下午 3 點提醒我開會」(單次絕對)

不需要主動推送的 agent **跳過整個 phase**。這是所有能力裡唯一「agent 不等使用者開口就動作」的,沒這需求別開。

## ⛔ 開工前必問(AskUserQuestion)

```python
AskUserQuestion(questions=[{
    "question": "要加排程提醒 / 主動推送嗎?(agent 會在未來主動發訊息給你)",
    "header": "Scheduler",
    "multiSelect": False,
    "options": [
        {"label": "要、含週期 cron", "description": "單次提醒 + 週期排程(每天/每週),APScheduler + sqlite 持久化"},
        {"label": "只要單次提醒", "description": "「N 分鐘後提醒我」這類,不做 cron,實作更簡單"},
        {"label": "不要", "description": "agent 維持純 reactive(只在你發訊息時回應)"},
    ],
}])
```

## ⚠️ Channel 依賴 — 開工前先講清楚

主動推送靠 channel 能不能「在使用者沒發訊息時 push」:

| Channel | 能真推? | 行為 |
|---|---|---|
| **Telegram** | ✅ | bot 是常駐 process、`bot.send_message(chat_id, ...)` 隨時推得到 |
| **Streamlit web** | ❌ | 請求-回應模型、分頁關了就沒 process 在跑、**推不到** |

**Streamlit-only 的退化方案**:reminders 照存 sqlite,但不主動跳出來;改成**使用者下次互動時、orchestrator 開頭先查「有沒有到期的 pending reminder」**,有就在回應前先報。這不是真 push、是「補檢查」,要在 Phase 7.5 channel gate 就跟使用者講明。

> 若使用者選 Web-only 又堅持要真 push → 需要額外架 notifier(系統通知 / email / 第三方),超出本 phase 範圍,標 `Partial` 等裁示。

## 架構:APScheduler(記憶體執行)+ sqlite(持久化)

- **APScheduler** 負責「時間到了觸發 callback」— 但它的 job 存記憶體、**重啟就沒**
- **sqlite** 是 source of truth — 重啟時從 sqlite 重建所有 pending job
- 不用 SQLAlchemy、不用 APScheduler 自家的 jobstore(避免額外 ORM 依賴);手動 sqlite + 啟動時 reload

```
pip install apscheduler>=3.10
```

### 1. sqlite 資料表(沿用既有 chat_store.db)

```sql
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    trigger_time INTEGER,          -- Unix ts(單次提醒;週期型留 NULL)
    cron         TEXT,             -- cron 表達式(週期;單次留 NULL)
    end_time     INTEGER,          -- Unix ts(週期任務的截止時間;到了自動 expired)
    message      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | cancelled | expired
    created_at   INTEGER NOT NULL
);
```

> `end_time` 是**週期任務的安全閥**:沒它的話、`*/15 * * * *` 會無限跑、誰都停不下來。詳見下方坑點三。

### 2. 核心管理器 `agent/scheduler.py`

```python
import time
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger


class ReminderManager:
    def __init__(self, db):
        self.db = db
        self._bot = None
        # ⛔ 坑點二:一定要鎖時區、不要讓 naive datetime 被當 UTC
        self.scheduler = AsyncIOScheduler(timezone="Asia/Taipei")

    def set_bot(self, bot):
        self._bot = bot

    def start(self):
        self.scheduler.start()
        self._reload_from_db()      # 重啟後恢復所有 pending

    def shutdown(self):
        self.scheduler.shutdown(wait=False)

    def _reload_from_db(self):
        rows = self.db.execute(
            "SELECT * FROM reminders WHERE status='pending'"
        ).fetchall()
        now = int(time.time())
        for r in rows:
            # ⛔ 坑點三:週期任務若已過 end_time、重啟時別再排、直接標 expired
            if r["end_time"] and r["end_time"] <= now:
                self.db.execute(
                    "UPDATE reminders SET status='expired' WHERE id=?", (r["id"],)
                )
                self.db.commit()
                continue

            if r["cron"]:
                trigger = CronTrigger.from_crontab(
                    r["cron"], timezone=self.scheduler.timezone,
                )
                if r["end_time"]:
                    # 綁 end_date — 排程器到期會自動在記憶體註銷此 job
                    trigger.end_date = datetime.fromtimestamp(
                        r["end_time"], tz=self.scheduler.timezone,
                    )
                self.scheduler.add_job(
                    self._fire, trigger, args=[r["id"]], id=f"rem_{r['id']}",
                )
            elif r["trigger_time"]:
                if r["trigger_time"] <= now:
                    # 已過期(bot 當機那段時間錯過的)→ 立即補發
                    self._fire_sync(r["id"])
                else:
                    self.scheduler.add_job(
                        self._fire, "date",
                        run_date=datetime.fromtimestamp(r["trigger_time"]),
                        args=[r["id"]], id=f"rem_{r['id']}",
                    )

    async def _fire(self, reminder_id):
        self._fire_sync(reminder_id)

    def _fire_sync(self, reminder_id):
        row = self.db.execute(
            "SELECT * FROM reminders WHERE id=?", (reminder_id,)
        ).fetchone()
        if not row or row["status"] != "pending":
            return
        if self._bot:
            import asyncio
            asyncio.create_task(
                self._bot.send_message(row["chat_id"], f"⏰ {row['message']}")
            )
        # 單次提醒發完標 sent;週期型保持 pending
        if not row["cron"]:
            self.db.execute(
                "UPDATE reminders SET status='sent' WHERE id=?", (reminder_id,)
            )
            self.db.commit()
```

### 3. LLM 工具 `agent/tools/scheduler_tools.py`

三個工具(注意全接 `**kwargs`、見 phase2 §kwargs-rule;`chat_id` 用 ContextVar 拿、不要 inject、見 phase11/14):

```python
import time, re
from datetime import datetime
from agent.user_context import current_chat_id   # ContextVar

def set_reminder(time_str: str, message: str, cron: str = None,
                 end_time_str: str = None, **kwargs) -> dict:
    """設一個提醒。time_str 支援:相對(10m/2h/1d)、絕對(YYYY-MM-DD HH:MM:SS)。
       要週期重複時傳 cron(如 '0 9 * * 1-5')、此時 time_str 給 '' 即可。
       週期任務要自動停止時、傳 end_time_str(同 time_str 格式)— 到期排程器自動註銷。"""
    chat_id = current_chat_id.get()
    trigger_ts = None
    if not cron:
        trigger_ts = _parse_time(time_str)        # 見下
        if trigger_ts is None:
            return {"error": f"看不懂時間:{time_str}。用 10m / 2h 或 YYYY-MM-DD HH:MM:SS"}

    end_ts = None
    if end_time_str:
        end_ts = _parse_time(end_time_str)
        if end_ts is None:
            return {"error": f"看不懂截止時間:{end_time_str}"}
    # ⛔ 坑點三:週期任務(cron)強烈建議帶 end_time、否則無限跑。
    #    若 LLM 設了 cron 卻沒 end_time、可在 system prompt 要求它主動問使用者「跑到何時?」
    ...  # INSERT into reminders(... end_time=end_ts ...), add_job(帶 trigger.end_date),
        # 回 {"ok": True, "reminder_id": id}

def list_reminders(**kwargs) -> dict:
    """列出當前 chat 所有 pending 提醒。"""
    ...

def cancel_reminder(reminder_id: int, **kwargs) -> dict:
    """取消一個提醒(status→cancelled、移除 scheduler job)。"""
    ...


def _parse_time(s: str) -> int | None:
    s = s.strip()
    m = re.fullmatch(r"(\d+)\s*([smhd])", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return int(time.time()) + n * mult
    try:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return None
```

## 🚨 三個架構地雷(必避)

### 💥 坑點一:Asyncio event loop 衝突(最嚴重)

**現象**:reminder 寫進 db 了、時間到卻**完全不觸發**、status 永遠卡 `pending`。

**原因**:`AsyncIOScheduler.start()` 會抓「當下執行緒正在跑的 event loop」。若你在 `app.run_polling()` **之前**同步 start 排程器,它綁到的是初始化階段的舊 loop;但 `python-telegram-bot` 跑 `run_polling()` 後會開一個**全新的 event loop** → 排程器綁在死掉的舊 loop 上、永遠不 fire。

**解法**:把排程器的生命週期**對齊 TG app 的 async lifecycle hook**,在 `post_init` 裡 start(此時 bot 的 loop 已 active):

```python
# telegram_adapter.py
def run(self):
    async def _post_init(app):
        from agent.scheduler import reminder_manager
        reminder_manager.set_bot(app.bot)
        reminder_manager.start()          # ★ 此時才 start、共享 bot 的 loop

    async def _post_shutdown(app):
        from agent.scheduler import reminder_manager
        reminder_manager.shutdown()

    self.app.post_init = _post_init
    self.app.post_shutdown = _post_shutdown
    self.app.run_polling(drop_pending_updates=True)
```

### 💥 坑點二:Naive datetime 時區錯亂

**現象**:Windows / Docker 容器上,提醒提早或延後數小時觸發。

**原因**:`datetime.fromtimestamp(ts)` 產出 naive datetime,APScheduler 預設可能當 UTC 換算、產生偏移。

**解法**:初始化排程器**顯式鎖時區**:

```python
self.scheduler = AsyncIOScheduler(timezone="Asia/Taipei")
```

容器內也要確保 tz 一致(Dockerfile 設 `ENV TZ=Asia/Taipei` + 裝 tzdata),否則 cron `0 9 * * 1-5` 的「9 點」會是容器的 9 點不是你的 9 點。

### 💥 坑點三:週期任務生命週期失效 + AI 認知斷層

**核心洞見(最容易被忽略)**:**排程器觸發時是直接呼 `bot.send_message`、不會進 AI 的 reasoning loop**。Scheduler fire ≠ agent thinking。

**現象**:使用者「每 15 分鐘提醒一次,中午 12 點停」。AI 助理常**自作聰明**:設一個 `*/15 * * * *` 週期任務 + 一個 12:00 的單次提醒「提醒自己去取消排程」。結果 12 點 bot 只發了一行純文字、**背景週期任務照樣無限跑** — 因為那行通知沒觸發 AI 去呼 `cancel_reminder`,使用者被疲勞轟炸。

**為什麼 AI 會錯**:LLM 把自己當成「會在 12 點醒來思考」的 agent。但排程器是 dumb timer、到點只送 message string、不喚醒 reasoning。**「設一個提醒叫自己做事」這種自我回呼的設計、在這個架構下不成立**。

**解法 — 截止時間綁在 Trigger 上、由排程器自動銷毀,不靠 AI 回來收尾**:

```python
trigger = CronTrigger.from_crontab(cron, timezone=self.scheduler.timezone)
if end_time:
    # 顯式 end_date → 排程器到期自動在記憶體註銷此 job、不需要任何人介入
    trigger.end_date = datetime.fromtimestamp(end_time, tz=self.scheduler.timezone)
self.scheduler.add_job(self._fire, trigger, args=[rid], id=f"rem_{rid}")
```

三道防線缺一不可:
1. **工具參數**:`set_reminder` 收 `end_time_str`(上面已加)
2. **Trigger.end_date**:排程器記憶體層自動到期銷毀(上面已加)
3. **重啟載入防禦**:`_reload_from_db` 發現 `now > end_time` 直接標 `expired`、不再排入(上面已加)— 否則 bot 重啟會把過期的週期任務又喚醒

**system prompt 要寫死**:
```
- 設「每隔 X 重複」的提醒時,如果使用者提到結束條件(「到中午」「持續一週」),
  必須用 set_reminder 的 end_time_str 參數,絕對不要另設一個提醒叫自己去取消。
- 排程提醒到點只會發訊息、不會喚醒你思考。任何「到時候自動做 OO」的需求,
  都要在設定當下就用參數表達完整,不能靠未來的你回來收尾。
```

## 跟其他 phase 的關係

- **Phase 7(permissions)**:reminder 只寫自己的 sqlite 表、不碰 FS,但 `set_reminder` 若被要求「到時候跑某個寫檔工具」就要過 permissions
- **Phase 5(two-step write)**:`cancel_reminder` 是破壞性操作、建議 `confirm` 二段;`set_reminder` 通常不必(可逆、列出來就能取消)
- **Phase 8 §9(/stop)**:`/stop` 只中斷當前 step loop、**不該**清掉已排程的 reminder(那是獨立生命週期)
- **Phase 12(self-evolution)**:agent 可能自己長出「每天早上彙整昨天 git log」這類複合排程工具,包 `set_reminder` + 既有工具

## 檢查清單

- [ ] AskUserQuestion 問了:要不要 / 含不含 cron
- [ ] Phase 7.5 已跟使用者講明:**Web-only 推不到、只能下次互動補檢查**
- [ ] `AsyncIOScheduler(timezone=...)` 顯式鎖時區(坑點二)
- [ ] 排程器在 `post_init` 內 start、不在 `run_polling` 前(坑點一)
- [ ] 重啟測試:設一個「2 分鐘後」提醒 → 重啟 bot → 確認 reload 後仍會觸發
- [ ] 過期補發測試:設「1 分鐘後」→ 立刻關 bot → 等 3 分鐘 → 開 bot → 確認立即補發
- [ ] **週期截止測試(坑點三)**:設「每分鐘、跑到 3 分鐘後止」→ 確認第 4 分鐘不再觸發、db status=expired
- [ ] **重啟過期防禦**:設週期 + 已過的 end_time → 重啟 → 確認標 expired、沒被重新排入
- [ ] **system prompt 已寫死**:週期任務有結束條件時用 end_time_str、不另設提醒叫自己取消
- [ ] 工具 signature 全接 `**kwargs`、`chat_id` 走 ContextVar 不 inject
