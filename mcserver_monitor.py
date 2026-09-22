#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCServerHost 多账号自动监控脚本（GitHub Actions + Xvfb · 纯净版）
- 多账号：MC_ACCOUNTS 填 JSON 数组，每账号独立浏览器实例（cookie 隔离）
- 兼容单号：未设置 MC_ACCOUNTS 时回退 MC_EMAIL / MC_PASSWORD
- 隐形 Turnstile：提交前主动触发交互签发 token
- 行为：关机自动开机；每轮巡检结束后发一条 TG 汇总（含所有账号结果 + 截图）
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from seleniumbase import SB

# ==================== 环境变量 ====================
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

LOGIN_URL = "https://mcserverhost.com/login"
SERVERS_URL = "https://mcserverhost.com/servers"

STATUS_OK = ("running", "starting", "stopping")
STATUS_OFF = ("offline", "stopped", "suspended")

def load_accounts():
    """读取多账号配置，兼容单号"""
    raw = os.environ.get("MC_ACCOUNTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("MC_ACCOUNTS 必须是 JSON 数组")
            accounts = []
            for i, item in enumerate(data):
                email = (item.get("email") or "").strip()
                password = (item.get("password") or "").strip()
                if not email or not password:
                    print(f"⚠️ 第 {i+1} 个账号缺少 email/password，已跳过", flush=True)
                    continue
                name = (item.get("name") or "").strip() or email.split("@")[0]
                accounts.append({"email": email, "password": password, "name": name})
            return accounts
        except Exception as e:
            print(f"❌ MC_ACCOUNTS JSON 解析失败: {e}", flush=True)
            return []

    email = os.environ.get("MC_EMAIL", "").strip()
    password = os.environ.get("MC_PASSWORD", "").strip()
    if email and password:
        return [{"email": email, "password": password, "name": email.split("@")[0]}]
    return []

# ==================== 基础工具 ====================

def log(account, msg):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{now}][{account}] {msg}", flush=True)

def tg_send(text, photo_path=None):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        print("⚠️ TG 未配置，跳过通知", flush=True)
        return
    try:
        if photo_path and os.path.exists(photo_path) and os.path.getsize(photo_path) > 1000:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as f:
                requests.post(url, data={"chat_id": TG_CHAT_ID, "caption": text,
                                         "parse_mode": "HTML"},
                              files={"photo": f}, timeout=30)
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text,
                                      "parse_mode": "HTML"}, timeout=30)
        print("  ✅ TG 汇总通知发送成功", flush=True)
    except Exception as e:
        print(f"  ⚠️ TG 通知失败: {e}", flush=True)

def screenshot(sb, name):
    try:
        sb.save_screenshot(name)
        return name
    except Exception:
        return None

def _safe(s):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(s))[:20]

# ==================== Cloudflare / Turnstile ====================

def is_cf_challenge(sb):
    try:
        for sel in ("#challenge-stage", "#challenge-running",
                    "#cf-challenge-running", "#cf-please-wait"):
            try:
                if sb.is_element_visible(sel):
                    return True
            except Exception:
                pass
        try:
            title = (sb.driver.title or "").lower()
            if "just a moment" in title or "attention required" in title:
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False

def nudge_turnstile(sb):
    """主动触发隐形 Turnstile 签发 token（本站登录必需），无框时内部自行处理"""
    try:
        sb.uc_gui_click_captcha()
    except Exception:
        pass
    time.sleep(2)

def bypass_hard_challenge(sb, name, max_retry=4):
    if not is_cf_challenge(sb):
        return True
    log(name, "🛡️ 检测到整页 Cloudflare 硬挑战，尝试通过...")
    for i in range(max_retry):
        nudge_turnstile(sb)
        time.sleep(3)
        if not is_cf_challenge(sb):
            log(name, f"   ✅ 硬挑战已通过（第 {i+1} 次）")
            return True
    log(name, "❌ 硬挑战未通过")
    return False

# ==================== 登录 ====================

SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button#login-btn",
    "form button",
    'button:contains("Login")',
    'button:contains("Sign in")',
    'button:contains("Log in")',
]

def dump_login_form(sb, name):
    try:
        info = sb.driver.execute_script("""
            const out = {url: location.href, inputs: [], buttons: [], turnstile: false};
            document.querySelectorAll('input').forEach(i => out.inputs.push({
                type: i.type, name: i.name, id: i.id,
                visible: i.offsetParent !== null, value_len: (i.value || '').length
            }));
            document.querySelectorAll('button').forEach(b => out.buttons.push({
                type: b.type, text: (b.innerText || '').trim().slice(0, 30),
                visible: b.offsetParent !== null, disabled: b.disabled
            }));
            out.turnstile = !!document.querySelector('[name*="turnstile"],iframe[src*="challenges.cloudflare.com"]');
            return JSON.stringify(out);
        """)
        log(name, "🔎 登录页诊断: " + json.dumps(json.loads(info), ensure_ascii=False))
    except Exception as e:
        log(name, f"🔎 诊断失败: {e}")

def click_submit(sb, name):
    for sel in SUBMIT_SELECTORS:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(name, f"   已点击提交按钮: {sel}")
                return True
        except Exception:
            continue
    try:
        did = sb.driver.execute_script("""
            const f = document.querySelector('form');
            if (!f) return false;
            if (f.requestSubmit) { f.requestSubmit(); return true; }
            f.submit();
            return true;
        """)
        if did:
            log(name, "   已通过 JS requestSubmit 提交表单")
            return True
    except Exception:
        pass
    return False

def login(sb, account):
    name = account["name"]
    log(name, "🌐 打开登录页...")
    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
    except Exception:
        sb.open(LOGIN_URL)
    try:
        sb.wait_for_ready_state_complete()
    except Exception:
        pass
    time.sleep(3)

    bypass_hard_challenge(sb, name)
    nudge_turnstile(sb)

    if "login" not in (sb.get_current_url() or "").lower():
        return True

    email_sel = "input[name='email'], input[type='email'], input[name='username']"
    pass_sel = "input[type='password'], input[name='password']"

    log(name, "🔑 填写凭据...")
    try:
        sb.wait_for_element_visible(email_sel, timeout=20)
        sb.clear(email_sel)
        sb.type(email_sel, account["email"])
        time.sleep(0.8)
        sb.clear(pass_sel)
        sb.type(pass_sel, account["password"])
        time.sleep(1.2)
        try:
            sb.execute_script(
                "document.querySelectorAll('input[type=\"checkbox\"]').forEach(c=>c.checked=true);")
        except Exception:
            pass
        time.sleep(1)
    except Exception as e:
        log(name, f"❌ 填写凭据失败: {e}")
        return False

    nudge_turnstile(sb)

    for attempt in range(5):
        log(name, f"🔑 提交登录（第 {attempt+1} 次）... 当前 URL: {sb.get_current_url()}")
        click_submit(sb, name)
        time.sleep(4)

        if is_cf_challenge(sb):
            bypass_hard_challenge(sb, name, max_retry=3)
        if "login" in (sb.get_current_url() or "").lower():
            nudge_turnstile(sb)

        for _ in range(8):
            if "login" not in (sb.get_current_url() or "").lower():
                log(name, f"✅ 登录成功 → {sb.get_current_url()}")
                return True
            time.sleep(1)

    log(name, "❌ 5 次尝试后仍在登录页")
    dump_login_form(sb, name)
    return False

# ==================== 服务器卡片识别 ====================

READ_CARDS_JS = r"""
const STATUSES = ['running','offline','stopped','starting','stopping','suspended'];
document.querySelectorAll('[data-mc-tag]').forEach(e => e.removeAttribute('data-mc-tag'));

const pills = [];
document.querySelectorAll('body *').forEach(el => {
    if (el.children.length === 0) {
        const t = (el.textContent || '').trim().toLowerCase();
        if (STATUSES.includes(t)) pills.push(el);
    }
});

const cards = [];
const seen = new Set();
pills.forEach((pill, idx) => {
    let card = pill;
    for (let i = 0; i < 8; i++) {
        const p = card.parentElement;
        if (!p) break;
        card = p;
        if (card.querySelectorAll('button').length >= 2 && i >= 2) break;
    }
    if (seen.has(card)) return;
    seen.add(card);

    let iconBtns = [...card.querySelectorAll('button')]
        .filter(b => b.querySelector('svg'))
        .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);

    const classify = (b) => {
        const h = (b.innerHTML || '').toLowerCase();
        const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.title || '')).toLowerCase();
        if (h.includes('lucide-play') || h.includes('data-lucide="play"')
            || h.includes('data-lucide=play') || label.includes('start')
            || label.includes('resume')) return 'play';
        if (h.includes('rotate') || h.includes('refresh') || label.includes('restart')
            || label.includes('reboot')) return 'restart';
        if (h.includes('lucide-square') || h.includes('data-lucide="square"')
            || h.includes('data-lucide=square') || label.includes('stop')
            || label.includes('shutdown')) return 'stop';
        return null;
    };

    const tags = iconBtns.map(classify);
    if (iconBtns.length === 3 && tags.some(t => t === null)) {
        ['play', 'restart', 'stop'].forEach((fb, i) => tags[i] = tags[i] || fb);
    }
    iconBtns.forEach((b, i) => {
        if (tags[i]) b.setAttribute('data-mc-tag', tags[i] + '-' + idx);
    });

    let name = '';
    const nameEl = card.querySelector('h1,h2,h3,h4,strong,b,a');
    if (nameEl) name = (nameEl.textContent || '').trim();
    if (!name) name = (card.innerText || '').split('\n').map(s => s.trim())
                      .find(s => s && !STATUSES.includes(s.toLowerCase())) || ('#' + (idx + 1));

    const playBtn = iconBtns.find((b, i) => tags[i] === 'play');
    cards.push({
        index: idx,
        name: name.slice(0, 60),
        status: (pill.textContent || '').trim().toLowerCase(),
        play_tagged: !!playBtn,
        play_disabled: playBtn ? (playBtn.disabled
            || playBtn.getAttribute('aria-disabled') === 'true') : null
    });
});
return cards;
"""

def read_cards(sb, name):
    try:
        return sb.driver.execute_script(READ_CARDS_JS) or []
    except Exception as e:
        log(name, f"⚠️ 读取卡片失败: {e}")
        return []

def click_play(sb, name, index):
    try:
        btn = sb.driver.find_element(By.CSS_SELECTOR, f"[data-mc-tag='play-{index}']")
    except Exception:
        return False
    try:
        sb.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        try:
            ActionChains(sb.driver).move_to_element(btn).pause(0.2).click().perform()
        except Exception:
            sb.driver.execute_script("arguments[0].click();", btn)
        return True
    except Exception as e:
        log(name, f"⚠️ 点击开机按钮异常: {e}")
        return False

def handle_confirm_dialog(sb, name):
    try:
        for b in sb.driver.find_elements(By.CSS_SELECTOR, "button"):
            if not b.is_displayed():
                continue
            t = (b.text or "").strip().lower()
            if t in ("confirm", "yes", "start", "ok", "continue") or "确认" in t or "启动" in t:
                sb.driver.execute_script("arguments[0].click();", b)
                log(name, f"   已确认弹窗：{t}")
                return True
    except Exception:
        pass
    return False

def check_and_start(sb, account):
    """返回 (code, servers, shots)
       code: ok_running / ok_started / fail_start / unknown
       servers: [(服务器名, 结果描述)]
       shots: 本次产生的截图路径列表
    """
    name = account["name"]
    shots = []
    log(name, "📡 打开服务器面板...")
    try:
        sb.uc_open_with_reconnect(SERVERS_URL, reconnect_time=5)
    except Exception:
        sb.open(SERVERS_URL)
    try:
        sb.wait_for_ready_state_complete()
    except Exception:
        pass
    time.sleep(5)
    bypass_hard_challenge(sb, name)

    cards = read_cards(sb, name)
    if not cards:
        log(name, "⚠️ 没有识别到任何服务器卡片")
        shots.append(screenshot(sb, f"{_safe(name)}_unknown.png"))
        return "unknown", [], shots

    offline = [c for c in cards if c["status"] in STATUS_OFF]
    if not offline:
        for c in cards:
            log(name, f"🟢 {c['name']} → {c['status']}，无需操作")
        return "ok_running", [(c["name"], "🟢 运行中，无需操作") for c in cards], shots

    results = []
    for c in offline:
        srv, idx = c["name"], c["index"]
        log(name, f"🔴 [{srv}] 离线，准备开机...")
        if not c["play_tagged"]:
            log(name, f"   ❌ [{srv}] 找不到开机按钮")
            results.append((c, False))
            continue

        shot = screenshot(sb, f"{_safe(name)}_before_{idx}.png")
        if shot:
            shots.append(shot)
        if not click_play(sb, name, idx):
            results.append((c, False))
            continue

        time.sleep(2)
        handle_confirm_dialog(sb, name)

        started = False
        for r in range(4):
            time.sleep(10)
            sb.driver.refresh()
            time.sleep(4)
            bypass_hard_challenge(sb, name)
            cur = {x["index"]: x for x in read_cards(sb, name)}.get(idx)
            cur_status = cur["status"] if cur else "unknown"
            log(name, f"   [{srv}] 第 {r+1} 次复查：{cur_status}")
            if cur_status in STATUS_OK:
                started = True
                break
        shot = screenshot(sb, f"{_safe(name)}_after_{idx}.png")
        if shot:
            shots.append(shot)
        results.append((c, started))

    servers = []
    for c, ok in results:
        servers.append((c["name"], "🟢 离线已自动开机" if ok else "🔴 开机失败/未确认"))
    code = "ok_started" if all(ok for _, ok in results) else "fail_start"
    return code, servers, shots

# ==================== 单账号流程（不单独发 TG，结果交给汇总） ====================

def run_account(account, seq, total):
    name = account["name"]
    print(f"\n{'='*50}\n▶️ 账号 {seq}/{total}：{name}（{account['email']}）\n{'='*50}", flush=True)

    out = {"name": name, "code": "error", "servers": [], "shots": []}
    sb_kwargs = {"uc": True, "headless": False, "ad_block": False, "locale_code": "en"}

    with SB(**sb_kwargs) as sb:
        try:
            sb.driver.set_page_load_timeout(45)

            if not login(sb, account):
                shot = screenshot(sb, f"{_safe(name)}_login_fail.png")
                if shot:
                    out["shots"].append(shot)
                out["code"] = "login_fail"
                out["servers"] = [("—", "🔴 登录失败，请查看日志诊断")]
                return out

            code, servers, shots = check_and_start(sb, account)
            out["code"] = code
            out["servers"] = servers
            out["shots"] = shots
            return out

        except Exception as e:
            log(name, f"❌ 运行异常: {e}")
            shot = screenshot(sb, f"{_safe(name)}_error.png")
            if shot:
                out["shots"].append(shot)
            out["code"] = "error"
            out["servers"] = [("—", f"🔴 脚本异常：{str(e)[:120]}")]
            return out

CODE_HEAD = {
    "ok_running": "🟢 全部运行中",
    "ok_started": "🟢 离线已开机",
    "fail_start": "🔴 开机异常",
    "unknown": "⚪ 状态未知",
    "login_fail": "🔴 登录失败",
    "error": "🔴 运行异常",
}

def build_summary(results, now_str):
    lines = ["📋 <b>MCServerHost 巡检完成</b>", ""]
    for r in results:
        head = CODE_HEAD.get(r["code"], r["code"])
        lines.append(f"👤 <b>{r['name']}</b>　{head}")
        for srv, desc in r["servers"]:
            lines.append(f"    🖥️ <code>{srv}</code>：{desc}")
        lines.append("")
    lines.append(f"⏰ <b>执行时间：</b><code>{now_str}</code>")
    return "\n".join(lines)

def pick_summary_photo(results):
    """截图优先级：开机成功后的截图 > 失败/异常截图"""
    for r in results:
        for p in r["shots"]:
            if "_after_" in p and os.path.exists(p):
                return p
    for r in results:
        for p in r["shots"]:
            if os.path.exists(p):
                return p
    return None

# ==================== 主流程 ====================

def main():
    print("=== MCServerHost 多账号自动巡检启动 ===", flush=True)

    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置有效账号：请设置 MC_ACCOUNTS（JSON 数组）或 MC_EMAIL/MC_PASSWORD", flush=True)
        return

    print(f"📋 共加载 {len(accounts)} 个账号："
          + ", ".join(a["name"] for a in accounts), flush=True)

    results = []
    for i, account in enumerate(accounts, 1):
        results.append(run_account(account, i, len(accounts)))
        if i < len(accounts):
            time.sleep(8)   # 账号间隔，降低风控

    # 控制台汇总
    print("\n" + "="*50, flush=True)
    print("📊 本次巡检汇总：", flush=True)
    for r in results:
        print(f"   {r['name']}: {r['code']}", flush=True)
    print("="*50, flush=True)

    # 巡检完成 → TG 汇总通知（每轮一条）
    now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    tg_send(build_summary(results, now_str), pick_summary_photo(results))

if __name__ == "__main__":
    main()
