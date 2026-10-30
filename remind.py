#!/usr/bin/env python3
"""GT7 Viewers Gift (觀賽領車) reminders -> Telegram.

Runs on GitHub Actions. Reads campaigns.json (dates are filled in by a weekly
Claude task), keeps progress in state.json, and:
  1. reads Telegram button presses ("✅ 已報名" / "✅ 已領")
     Claim reminders are only sent for campaigns she marked as watched.
  2. sends reminders on the right Perth dates, each only once.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Testing: DRY_RUN=1 (print instead of send), NOW=2026-11-02T07:00 (Perth time)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PERTH = ZoneInfo("Australia/Perth")
HERE = os.path.dirname(os.path.abspath(__file__))
CAMPAIGNS_FILE = os.path.join(HERE, "campaigns.json")
STATE_FILE = os.path.join(HERE, "state.json")

CLAIM_WINDOW_DAYS = 14      # used when the official claim deadline is unknown
DIST_REMINDER_HOUR = 15     # gifts usually land in the afternoon Perth time
WEEKDAYS = "一二三四五六日"

DRY_RUN = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()


# ---------- helpers ----------
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def to_perth(iso):
    """'2026-10-19T08:59:00Z' -> aware Perth datetime."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(PERTH)


def fmt_date(d):
    return f"{d.month}/{d.day}（{WEEKDAYS[d.weekday()]}）"


def fmt_dt(dt):
    return f"{fmt_date(dt.date())} {dt:%H:%M}"


def now_perth():
    override = os.environ.get("NOW")
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=PERTH)
    return datetime.now(PERTH)


def tg(method, **params):
    if DRY_RUN:
        print(f"[DRY_RUN] {method}: {json.dumps(params, ensure_ascii=False)}")
        return {"ok": True, "result": []}
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(
        {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
    ).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.loads(r.read())


def send(text, buttons=None):
    params = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": [buttons]}
    res = tg("sendMessage", **params)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {res}")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- campaign dates ----------
def claim_info(c):
    """Return (distribution_date, deadline_date, deadline_dt_or_None)."""
    dd = date.fromisoformat(c["distribution_date"])
    if c.get("claim_deadline"):
        cd_dt = to_perth(c["claim_deadline"])
        return dd, cd_dt.date(), cd_dt
    return dd, dd + timedelta(days=CLAIM_WINDOW_DAYS), None


def deadline_text(c):
    _, cd, cd_dt = claim_info(c)
    if cd_dt:
        return f"⏰ 領取截止：<b>{fmt_dt(cd_dt)}</b>（柏斯）"
    return (f"⏰ 領取截止：約 <b>{fmt_date(cd)}</b>"
            f"（估計：派發後 {CLAIM_WINDOW_DAYS} 天，以遊戲內顯示為準）")


def header_block(c):
    lines = [f"<b>{esc(c['name'])}</b>"]
    if c.get("rewards"):
        lines.append("🚗 " + "、".join(esc(r) for r in c["rewards"]))
    return lines


def footer(c):
    lines = []
    if c.get("source_url"):
        lines.append(f'<a href="{c["source_url"]}">官方公告</a>')
    lines.append("按下方按鈕後，這站就不會再提醒。")
    return lines


# ---------- Telegram button presses ----------
def process_updates(state, campaigns):
    by_id = {c["id"]: c for c in campaigns}
    res = tg("getUpdates", offset=state.get("tg_offset", 0), timeout=0,
             allowed_updates=["callback_query", "message"])
    if not res.get("ok"):
        print(f"getUpdates failed: {res}", file=sys.stderr)
        return
    for upd in res.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue
        msg = cq.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
            continue
        action, _, cid = (cq.get("data") or "").partition("|")
        c = by_id.get(cid)
        if not c:
            continue
        if action == "e":
            bucket, reply = "entered", "✅ 收到：已報名。之後獎勵送到車庫時會提醒你領。"
        elif action == "c":
            bucket, reply = "claimed", "✅ 收到：已領取，這站不再提醒。"
        else:
            continue
        if cid not in state[bucket]:
            state[bucket].append(cid)
        try:
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="收到")
        except Exception:
            pass  # too old to answer; harmless
        try:
            tg("editMessageReplyMarkup", chat_id=CHAT_ID,
               message_id=msg.get("message_id"), reply_markup={"inline_keyboard": []})
        except Exception:
            pass
        send(f"{reply}\n<b>{esc(c['name'])}</b>")


# ---------- reminders ----------
def pick_due(kinds, sent_keys, cid):
    """kinds: list of (kind, is_due). Return (latest due kind, all due kinds)."""
    due = [k for k, ok in kinds if ok and f"{cid}:{k}" not in sent_keys]
    return (due[-1] if due else None), due


def entry_reminder(c, now, state):
    cid = c["id"]
    if cid in state["entered"] or not c.get("entry_start") or not c.get("entry_end"):
        return
    es = to_perth(c["entry_start"])
    ee = to_perth(c["entry_end"])
    today = now.date()
    if now >= ee or today < es.date():
        return
    kinds = [
        ("entry_start", es.date() <= today),
        ("entry_5d", ee.date() - timedelta(days=5) <= today),
        ("entry_last", ee.date() <= today),
    ]
    latest, due = pick_due(kinds, set(state["sent"]), cid)
    if not latest:
        return
    title = {
        "entry_start": "🏁 <b>GT7 觀賽領車開始！</b>",
        "entry_5d": "⏰ <b>GT7 觀賽領車：報名剩 5 天</b>",
        "entry_last": "🚨 <b>GT7 觀賽領車：今天截止！</b>",
    }[latest]
    lines = [title, *header_block(c),
             f"⏰ 報名截止：<b>{fmt_dt(ee)}</b>（柏斯）",
             "👉 世界地圖右上角「VIEWERS Campaign」→ 點開 Manufacturers Cup 和 Nations Cup 兩個直播，各看幾秒（兩個都要點）",
             *footer(c)]
    send("\n".join(lines), [{"text": "✅ 已報名", "callback_data": f"e|{cid}"}])
    state["sent"].extend(f"{cid}:{k}" for k in due)


def claim_reminder(c, now, state):
    cid = c["id"]
    # Only remind to claim if she watched (pressed "✅ 已報名"); no watch = no gift.
    if cid not in state["entered"] or cid in state["claimed"] or not c.get("distribution_date"):
        return
    dd, cd, _ = claim_info(c)
    today = now.date()
    if today > cd:
        return
    dist_due = dd < today or (dd == today and now.hour >= DIST_REMINDER_HOUR)
    kinds = [
        ("dist", dist_due),
        ("claim_1d", cd - timedelta(days=1) <= today),
    ]
    latest, due = pick_due(kinds, set(state["sent"]), cid)
    if not latest:
        return
    title = {
        "dist": "🎁 <b>GT7 獎勵已送到車庫！</b>",
        "claim_1d": "🚨 <b>GT7 獎勵明天截止領取！</b>",
    }[latest]
    lines = [title, *header_block(c),
             "📍 車庫 →「禮物」分頁",
             deadline_text(c), *footer(c)]
    send("\n".join(lines), [{"text": "✅ 已領", "callback_data": f"c|{cid}"}])
    state["sent"].extend(f"{cid}:{k}" for k in due)


def main():
    if not DRY_RUN and (not TOKEN or not CHAT_ID):
        sys.exit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets.")
    campaigns = load(CAMPAIGNS_FILE, {"campaigns": []})["campaigns"]
    state = load(STATE_FILE, {})
    for key in ("entered", "claimed", "sent"):
        state.setdefault(key, [])
    state.setdefault("tg_offset", 0)

    if os.environ.get("TEST_MESSAGE") == "true":
        send("✅ <b>GT7 提醒測試成功</b>\n之後的觀賽領車提醒都會發到這裡。")
        print("Test message sent.")
        return

    now = now_perth()
    print(f"Run at {now:%Y-%m-%d %H:%M} Perth, {len(campaigns)} campaign(s)")

    errors = []
    try:
        process_updates(state, campaigns)
    except Exception as e:
        errors.append(f"updates: {e}")
    for c in campaigns:
        for fn in (entry_reminder, claim_reminder):
            try:
                fn(c, now, state)
            except Exception as e:
                errors.append(f"{c.get('id')}/{fn.__name__}: {e}")

    save(STATE_FILE, state)
    if errors:
        sys.exit("Errors:\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
