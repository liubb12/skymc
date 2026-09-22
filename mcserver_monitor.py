#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCServerHost 自动监控脚本（GitHub Actions + Xvfb · 无代理纯净版）
- 隐形 Turnstile：提交前主动触发交互签发 token（本站登录必需）
- 运行中/启动中静默退出；离线才点该卡片的开机按钮，复查确认后发 TG
"""

import os
import time
import json
import requests
from datetime import datetime, timedelta, timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from seleniumbase import SB

# ==================== 环境变量 ====================
MC_EMAIL = os.environ.get("MC_EMAIL", "").strip()
MC_PASSWORD = os.environ.get("MC_PASSWORD", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

LOGIN_URL = "https://mcserverhost.com/login"
SERVERS_URL = "https://mcserverhost.com/servers"

STATUS_OK = ("running", "starting", "stopping")
STATUS_OFF = ("offline", "stopped", "suspended")

# ==================== 基础工具 ====================

def log(msg):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def tg_send(text, photo_path=None):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log("⚠️ TG 未配置，跳过通知")
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
        log("✅ TG 通知发送成功")
    except Exception as e:
        log(f"⚠️ TG 通知失败: {e}")

def screenshot(sb, name="mc.png"):
    try:
        sb.save_screenshot(name)
        return name
    except Exception:
        return None

# ==================== Cloudflare / Turnstile ====================

def is_cf_challenge(sb):
    """只判断是否出现【整页可见】的硬挑战（用于日志提示）"""
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
    """主动与 Turnstile 交互一次。
    本站登录表单依赖 Turnstile token，隐形模式也必须触发它才签发；
    找不到复选框时 SeleniumBase 内部会自行处理 iframe，异常直接忽略。"""
    try:
        sb.uc_gui_click_captcha()
    except Exception:
        pass
    time.sleep(2)

def bypass_hard_challenge(sb, max_retry=4):
    """整页硬挑战时反复尝试"""
    if not is_cf_challenge(sb):
        return True
    log("🛡️ 检测到整页 Cloudflare 硬挑战，尝试通过...")
    for i in range(max_retry):
        nudge_turnstile(sb)
        time.sleep(3)
        if not is_cf_challenge(sb):
            log(f"   ✅ 硬挑战已通过（第 {i+1} 次）")
            return True
    log("❌ 硬挑战未通过")
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

def dump_login_form(sb):
    """登录失败时把表单结构打到日志，方便下次定位"""
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
        log("🔎 登录页诊断: " + json.dumps(json.loads(info), ensure_ascii=False))
    except Exception as e:
        log(f"🔎 诊断失败: {e}")

def click_submit(sb):
    """依次尝试候选选择器，返回是否点中"""
    for sel in SUBMIT_SELECTORS:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                log(f"   已点击提交按钮: {sel}")
                return True
        except Exception:
            continue
    # JS 兜底：requestSubmit 会正常触发表单的 submit 事件
    try:
        did = sb.driver.execute_script("""
            const f = document.querySelector('form');
            if (!f) return false;
            if (f.requestSubmit) { f.requestSubmit(); return true; }
            f.submit();
            return true;
        """)
        if did:
            log("   已通过 JS requestSubmit 提交表单")
            return True
    except Exception:
        pass
    return False

def login(sb):
    log("🌐 打开登录页...")
    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
    except Exception:
        sb.open(LOGIN_URL)
    try:
        sb.wait_for_ready_state_complete()
    except Exception:
        pass
    time.sleep(3)

    bypass_hard_challenge(sb)
    nudge_turnstile(sb)   # 进页面先戳一次隐形 Turnstile

    if "login" not in (sb.get_current_url() or "").lower():
        return True

    email_sel = "input[name='email'], input[type='email'], input[name='username']"
    pass_sel = "input[type='password'], input[name='password']"

    log("🔑 填写凭据...")
    try:
        sb.wait_for_element_visible(email_sel, timeout=20)
        sb.clear(email_sel)
        sb.type(email_sel, MC_EMAIL)
        time.sleep(0.8)
        sb.clear(pass_sel)
        sb.type(pass_sel, MC_PASSWORD)
        time.sleep(1.2)
        try:
            sb.execute_script(
                "document.querySelectorAll('input[type=\"checkbox\"]').forEach(c=>c.checked=true);")
        except Exception:
            pass
        time.sleep(1)
    except Exception as e:
        log(f"❌ 填写凭据失败: {e}")
        return False

    # 提交前再戳一次 Turnstile，确保 token 已挂到表单
    nudge_turnstile(sb)

    for attempt in range(5):
        log(f"🔑 提交登录（第 {attempt+1} 次）... 当前 URL: {sb.get_current_url()}")
        click_submit(sb)
        time.sleep(4)

        if is_cf_challenge(sb):
            bypass_hard_challenge(sb, max_retry=3)
        # 每轮再戳一次，给 token 刷新 + 表单重新提交的机会
        if "login" in (sb.get_current_url() or "").lower():
            nudge_turnstile(sb)

        # URL 轮询确认
        for _ in range(8):
            if "login" not in (sb.get_current_url() or "").lower():
                log(f"✅ 登录成功 → {sb.get_current_url()}")
                return True
            time.sleep(1)

    log("❌ 5 次尝试后仍在登录页")
    dump_login_form(sb)
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

def read_cards(sb):
    try:
        return sb.driver.execute_script(READ_CARDS_JS) or []
    except Exception as e:
        log(f"⚠️ 读取卡片失败: {e}")
        return []

def click_play(sb, index):
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
        log(f"⚠️ 点击开机按钮异常: {e}")
        return False

def handle_confirm_dialog(sb):
    try:
        for b in sb.driver.find_elements(By.CSS_SELECTOR, "button"):
            if not b.is_displayed():
                continue
            t = (b.text or "").strip().lower()
            if t in ("confirm", "yes", "start", "ok", "continue") or "确认" in t or "启动" in t:
                sb.driver.execute_script("arguments[0].click();", b)
                log(f"   已确认弹窗：{t}")
                return True
    except Exception:
        pass
    return False

def check_and_start(sb):
    log("📡 打开服务器面板...")
    try:
        sb.uc_open_with_reconnect(SERVERS_URL, reconnect_time=5)
    except Exception:
        sb.open(SERVERS_URL)
    try:
        sb.wait_for_ready_state_complete()
    except Exception:
        pass
    time.sleep(5)
    bypass_hard_challenge(sb)

    cards = read_cards(sb)
    if not cards:
        log("⚠️ 没有识别到任何服务器卡片")
        screenshot(sb, "mc_unknown.png")
        return "unknown", []

    for c in cards:
        log(f"   🖥️ {c['name']} → {c['status']}（开机按钮: {c['play_tagged']}, disabled: {c['play_disabled']}）")

    offline = [c for c in cards if c["status"] in STATUS_OFF]
    if not offline:
        log("🟢 所有服务器运行中/启动中，无需操作")
        return "ok_running", cards

    results = []
    for c in offline:
        name, idx = c["name"], c["index"]
        log(f"🔴 [{name}] 离线，准备开机...")
        if not c["play_tagged"]:
            log(f"   ❌ [{name}] 找不到开机按钮")
            results.append((c, False))
            continue

        screenshot(sb, f"mc_before_{idx}.png")
        if not click_play(sb, idx):
            results.append((c, False))
            continue

        time.sleep(2)
        handle_confirm_dialog(sb)

        started = False
        for r in range(4):
            time.sleep(10)
            sb.driver.refresh()
            time.sleep(4)
            bypass_hard_challenge(sb)
            cur = {x["index"]: x for x in read_cards(sb)}.get(idx)
            cur_status = cur["status"] if cur else "unknown"
            log(f"   [{name}] 第 {r+1} 次复查：{cur_status}")
            if cur_status in STATUS_OK:
                started = True
                break
        screenshot(sb, f"mc_after_{idx}.png")
        results.append((c, started))

    return ("fail_start" if any(not ok for _, ok in results) else "ok_started"), results

# ==================== 主流程 ====================

def main():
    log("=== MCServerHost 自动巡检启动（纯净版）===")

    if not MC_EMAIL or not MC_PASSWORD:
        log("❌ 未配置 MC_EMAIL / MC_PASSWORD")
        return

    sb_kwargs = {
        "uc": True,
        "headless": False,   # 配合 Xvfb
        "ad_block": False,
        "locale_code": "en",
    }

    with SB(**sb_kwargs) as sb:
        try:
            sb.driver.set_page_load_timeout(45)

            if not login(sb):
                shot = screenshot(sb, "mc_error.png")
                tg_send("🔴 <b>MCServerHost 登录失败</b>\n请查看 Actions 日志中的登录页诊断。", shot)
                return

            code, data = check_and_start(sb)
            now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

            if code == "ok_running":
                log("✅ 巡检完毕：全部正常，静默退出")
                return

            if code == "ok_started":
                names = "\n".join(f"🖥️ <code>{c['name']}</code>" for c, _ in data)
                pic = next((f"mc_after_{c['index']}.png" for c, _ in data
                            if os.path.exists(f"mc_after_{c['index']}.png")), None)
                tg_send(f"🟢 <b>MCServerHost 已自动开机</b>\n\n{names}\n\n"
                        f"⏰ <b>执行时间：</b><code>{now_str}</code>", pic)
                log("✅ 离线机器已全部开机，TG 已通知")
                return

            if code == "fail_start":
                ok_names = [c["name"] for c, ok in data if ok]
                bad_names = [c["name"] for c, ok in data if not ok]
                lines = []
                if ok_names:
                    lines.append("✅ 已开机：" + ", ".join(ok_names))
                if bad_names:
                    lines.append("❌ 开机失败：" + ", ".join(bad_names))
                pic = next((f"mc_before_{c['index']}.png" for c, ok in data
                            if not ok and os.path.exists(f"mc_before_{c['index']}.png")), None)
                tg_send("🔴 <b>MCServerHost 开机异常</b>\n\n"
                        + "\n".join(f"<code>{l}</code>" for l in lines)
                        + f"\n\n⏰ <b>执行时间：</b><code>{now_str}</code>", pic)
                return

            if code == "unknown":
                tg_send(f"⚪ <b>MCServerHost 状态未知</b>\n未识别到服务器卡片，可能界面改版。\n\n"
                        f"⏰ <code>{now_str}</code>",
                        "mc_unknown.png" if os.path.exists("mc_unknown.png") else None)

        except Exception as e:
            log(f"❌ 运行异常: {e}")
            shot = screenshot(sb, "mc_error.png")
            tg_send(f"🔴 <b>MCServerHost 脚本运行异常</b>\n\n<code>{str(e)[:500]}</code>", shot)

if __name__ == "__main__":
    main()
