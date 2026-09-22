#!/usr/bin/env python3

# -*- coding: utf-8 -*-
"""MCServerHost 自动监控脚本（GitHub Actions + Xvfb）
界面结构：服务器卡片 = 名称 + 状态胶囊(running/offline/...) + [▶开机][↻重启][■关机]
逻辑：

- 全部服务器运行中/启动中 → 静默退出

- 任一服务器离线 → 只点该卡片的【开机】按钮，复查确认后发 TG

- 登录失败 / 无法识别状态 / 开机未确认 → 发 TG 告警
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from seleniumbase import SB

# === 环境变量 ===
MC_EMAIL = os.environ.get("MC_EMAIL", "").strip()
MC_PASSWORD = os.environ.get("MC_PASSWORD", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

LOGIN_URL = "https://mcserverhost.com/login"
SERVERS_URL = "https://mcserverhost.com/servers"

STATUS_OK = ("running", "starting", "stopping")
STATUS_OFF = ("offline", "stopped", "suspended")

def log(msg):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def tg_send(text, photo_path=None):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log("⚠️ TG 未配置，跳过通知")
        return
    try:
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as f:
                requests.post(url, data={
                    "chat_id": TG_CHAT_ID,
                    "caption": text,
                    "parse_mode": "HTML",
                }, files={"photo": f}, timeout=30)
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            requests.post(url, data={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            }, timeout=30)
        log("✅ TG 通知发送成功")
    except Exception as e:
        log(f"⚠️ TG 通知失败: {e}")

def screenshot(sb, name="mc.png"):
    try:
        sb.save_screenshot(name)
        return name
    except Exception:
        return None

def is_cf_challenge(sb):
    """只在 Cloudflare 挑战【真正可见】时返回 True，避免隐形 Turnstile 误报"""
    try:
        for sel in ("#challenge-stage", "#challenge-running",
                    "#cf-challenge-running", "#cf-please-wait"):
            try:
                if sb.is_element_visible(sel):
                    return True
            except Exception:
                pass
        try:
            frames = sb.driver.find_elements(
                By.CSS_SELECTOR,
                "iframe[src*='challenges.cloudflare.com']"
            )
            for f in frames:
                try:
                    if (f.is_displayed()
                            and f.size.get("width", 0) > 100
                            and f.size.get("height", 0) > 40):
                        return True
                except Exception:
                    pass
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

def bypass_cf(sb, max_retry=4):
    if not is_cf_challenge(sb):
        return True
    log("🛡️ 检测到 Cloudflare 验证，尝试通过...")
    for i in range(max_retry):
        try:
            sb.uc_gui_click_captcha()
            time.sleep(4)
            if not is_cf_challenge(sb):
                log(f"   ✅ CF 验证已通过（第 {i+1} 次）")
                return True
        except Exception as e:
            log(f"   第 {i+1} 次尝试失败: {e}")
        time.sleep(2)
    log("❌ CF 验证未通过")
    return False

def login(sb):
    log("🌐 打开登录页...")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=4)
    time.sleep(3)
    bypass_cf(sb)

    if "login" not in sb.get_current_url().lower():
        return True

    log("🔑 填写凭据...")
    try:
        email_sel = "input[name='email'], input[type='email'], input[name='username']"
        pass_sel = "input[type='password'], input[name='password']"

        sb.wait_for_element_visible(email_sel, timeout=15)
        sb.clear(email_sel)
        sb.type(email_sel, MC_EMAIL)
        time.sleep(0.8)
        sb.clear(pass_sel)
        sb.type(pass_sel, MC_PASSWORD)
        time.sleep(1.2)

        try:
            sb.execute_script(
                "document.querySelectorAll('input[type=\"checkbox\"]').forEach(c=>c.checked=true);"
            )
        except Exception:
            pass
        time.sleep(0.5)

        submit_sel = "button[type='submit'], button#login-btn"
        for _ in range(3):
            try:
                sb.uc_click(submit_sel)
            except Exception:
                try:
                    sb.click(submit_sel)
                except Exception:
                    pass
            time.sleep(3)
            bypass_cf(sb, max_retry=3)
            if "login" not in sb.get_current_url().lower():
                break

        if "login" not in sb.get_current_url().lower():
            log(f"✅ 登录成功 → {sb.get_current_url()}")
            return True
        log("❌ 登录后仍在登录页")
        return False
    except Exception as e:
        log(f"❌ 表单交互异常: {e}")
        return False

# ---------- 服务器卡片识别（针对真实界面） ----------

READ_CARDS_JS = r"""
const STATUSES = ['running','offline','stopped','starting','stopping','suspended'];

// 清掉上一轮打的标记
document.querySelectorAll('[data-mc-tag]').forEach(e => e.removeAttribute('data-mc-tag'));

// 1) 找状态胶囊：叶子元素、文本恰好是状态词
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
    // 2) 向上找到包含按钮组的卡片容器
    let card = pill;
    for (let i = 0; i < 8; i++) {
        const p = card.parentElement;
        if (!p) break;
        card = p;
        if (card.querySelectorAll('button').length >= 2 && i >= 2) break;
    }
    if (seen.has(card)) return;
    seen.add(card);

    // 3) 卡片里只取图标按钮（含 svg），按从左到右排序：界面固定为 开机/重启/关机
    let iconBtns = [...card.querySelectorAll('button')]
        .filter(b => b.querySelector('svg'))
        .sort((a, b) => a.getBoundingClientRect().x - b.getBoundingClientRect().x);

    const classify = (b) => {
        const h = (b.innerHTML || '').toLowerCase();
        const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.title || '')).toLowerCase();
        if (h.includes('lucide-play') || h.includes('data-lucide="play"')
            || h.includes('data-lucide=play') || label.includes('start')
            || label.includes('resume')) return 'play';
        if (h.includes('rotate') || h.includes('refresh')
            || h.includes('lucide-restart') || label.includes('restart')
            || label.includes('reboot')) return 'restart';
        if (h.includes('lucide-square') || h.includes('data-lucide="square"')
            || h.includes('data-lucide=square') || label.includes('stop')
            || label.includes('shutdown')) return 'stop';
        return null;
    };

    const tags = iconBtns.map(classify);

    // 兜底：三个图标按钮且没全部识别出来时，按位置认定 [play, restart, stop]
    if (iconBtns.length === 3 && tags.some(t => t === null)) {
        const posFallback = ['play', 'restart', 'stop'];
        for (let i = 0; i < 3; i++) tags[i] = tags[i] || posFallback[i];
    }

    iconBtns.forEach((b, i) => {
        if (tags[i]) b.setAttribute('data-mc-tag', tags[i] + '-' + idx);
    });

    // 服务器名：卡片内第一段粗体标题文本
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
        play_disabled: playBtn ? (playBtn.disabled || playBtn.getAttribute('aria-disabled') === 'true') : null
    });
});
return cards;
"""

def read_cards(sb):
    """返回 [{index, name, status, play_tagged, play_disabled}]"""
    try:
        return sb.driver.execute_script(READ_CARDS_JS) or []
    except Exception as e:
        log(f"⚠️ 读取卡片失败: {e}")
        return []

def click_play(sb, index):
    """精确点击指定卡片上被标记为 play 的按钮"""
    sel = f"[data-mc-tag='play-{index}']"
    try:
        btn = sb.driver.find_element(By.CSS_SELECTOR, sel)
    except Exception:
        return False
    try:
        sb.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", btn)
        time.sleep(0.5)
        try:
            ActionChains(sb.driver).move_to_element(btn).click().perform()
        except Exception:
            sb.driver.execute_script("arguments[0].click();", btn)
        return True
    except Exception as e:
        log(f"⚠️ 点击开机按钮异常: {e}")
        return False

def handle_confirm_dialog(sb):
    """部分面板点开机后会弹确认框，尝试点确认"""
    try:
        btns = sb.driver.find_elements(By.CSS_SELECTOR, "button")
        for b in btns:
            if not b.is_displayed():
                continue
            t = (b.text or "").strip().lower()
            if t in ("confirm", "yes", "start", "ok", "continue") or "确认" in t or "启动" in t or "开机" in t:
                sb.driver.execute_script("arguments[0].click();", b)
                log(f"   已在确认弹窗点击：{t}")
                return True
    except Exception:
        pass
    return False

def check_and_start(sb):
    """返回 (code, details)
       ok_running  全部正常（静默）
       ok_started  有离线机器且已成功开机
       fail_start  离线但开机失败/未确认
       unknown     读不到任何服务器卡片
    """
    log("📡 打开服务器面板...")
    try:
        sb.uc_open_with_reconnect(SERVERS_URL, reconnect_time=4)
    except Exception:
        sb.open(SERVERS_URL)
    time.sleep(5)
    bypass_cf(sb, max_retry=2)

    cards = read_cards(sb)
    if not cards:
        log("⚠️ 页面上没有识别到任何服务器卡片")
        screenshot(sb, "mc_unknown.png")
        return "unknown", []

    for c in cards:
        log(f"   🖥️ {c['name']} → {c['status']}（开机按钮: {c['play_tagged']}, disabled: {c['play_disabled']}）")

    offline = [c for c in cards if c["status"] in STATUS_OFF]
    if not offline:
        log("🟢 所有服务器均在运行/启动中，无需操作")
        return "ok_running", cards

    # 对每台离线机器执行开机
    results = []  # (card, started_bool)
    for c in offline:
        name, idx = c["name"], c["index"]
        log(f"🔴 [{name}] 离线，准备开机...")

        if not c["play_tagged"]:
            log(f"   ❌ [{name}] 没找到开机按钮")
            results.append((c, False))
            continue

        screenshot(sb, f"mc_before_{idx}.png")
        if not click_play(sb, idx):
            results.append((c, False))
            continue

        time.sleep(2)
        handle_confirm_dialog(sb)

        # 复查最多 4 轮（约 48 秒），确认状态脱离离线
        started = False
        for r in range(4):
            time.sleep(10)
            sb.driver.refresh()
            time.sleep(4)
            bypass_cf(sb, max_retry=2)
            now_cards = {x["index"]: x for x in read_cards(sb)}
            cur = now_cards.get(idx)
            cur_status = cur["status"] if cur else "unknown"
            log(f"   [{name}] 第 {r+1} 次复查：{cur_status}")
            if cur_status in STATUS_OK:
                started = True
                break
        screenshot(sb, f"mc_after_{idx}.png")
        results.append((c, started))

    failed = [c for c, ok in results if not ok]
    if failed:
        return "fail_start", results
    return "ok_started", results

def main():
    log("=== MC
