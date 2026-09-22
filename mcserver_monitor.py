#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCServerHost 自动监控脚本（代理增强版 · GitHub Actions + Xvfb）
过盾手段（综合 SkyMC v18 经验）：
1. 优先：PROXY_SERVER 直接代理（http/socks5），把出口 IP 换成干净 IP
2. 或：NODE_LINK（vmess:// / vless://）自动拉起本地 sing-box 再代理
3. UC 模式 + reconnect + 隐形盾识别（只认真正可见的挑战）
4. 登录 5 次重试 + 盾消失轮询

业务逻辑：
- 全部服务器运行中/启动中 → 静默退出
- 任一离线 → 点该卡片的【开机】，复查确认后发 TG
- 登录失败 / 开机失败 / 状态未知 / 异常 → 发 TG 告警
"""

import atexit
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import requests
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from seleniumbase import SB

# ==================== 环境变量 ====================
MC_EMAIL = os.environ.get("MC_EMAIL", "").strip()
MC_PASSWORD = os.environ.get("MC_PASSWORD", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

# 代理二选一：
#   PROXY_SERVER 直接填 http://user:pass@host:port 或 socks5://host:port
#   NODE_LINK   填 vmess:// 或 vless:// 订阅链接（需要 runner 已装 sing-box）
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()
NODE_LINK = (os.environ.get("NODE_LINK") or "").strip()
SINGBOX_PORT = int(os.environ.get("SINGBOX_PORT") or "7890")

LOGIN_URL = "https://mcserverhost.com/login"
SERVERS_URL = "https://mcserverhost.com/servers"

STATUS_OK = ("running", "starting", "stopping")
STATUS_OFF = ("offline", "stopped", "suspended")

_singbox_proc = None
REQUESTS_PROXIES = None

# ==================== 代理（借自 SkyMC v18） ====================

def _b64decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    pad = (-len(data)) % 4
    return base64.b64decode(data + ("=" * pad))

def _parse_vmess(link: str) -> dict:
    obj = json.loads(_b64decode(link[len("vmess://"):]).decode("utf-8"))
    host = obj.get("add") or obj.get("host") or ""
    port = int(obj.get("port") or 443)
    outbound = {
        "type": "vmess", "tag": "proxy",
        "server": host, "server_port": port,
        "uuid": obj.get("id") or "",
        "security": obj.get("scy") or "auto",
        "alter_id": int(obj.get("aid") or 0),
    }
    if str(obj.get("tls") or "").lower() in ("tls", "reality", "1", "true"):
        outbound["tls"] = {
            "enabled": True,
            "server_name": obj.get("sni") or obj.get("host") or host,
            "insecure": False,
            "utls": {"enabled": True, "fingerprint": obj.get("fp") or "chrome"},
        }
    net = (obj.get("net") or "tcp").lower()
    if net == "ws":
        outbound["transport"] = {"type": "ws", "path": obj.get("path") or "/",
                                 "headers": {"Host": obj.get("host") or host}}
    elif net == "grpc":
        outbound["transport"] = {"type": "grpc", "service_name": obj.get("path") or ""}
    return outbound

def _parse_vless(link: str) -> dict:
    p = urlparse(link)
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    outbound = {
        "type": "vless", "tag": "proxy",
        "server": p.hostname or "", "server_port": int(p.port or 443),
        "uuid": unquote(p.username or ""),
        "flow": q.get("flow") or "", "packet_encoding": "xudp",
    }
    if (q.get("security") or "none").lower() in ("tls", "reality"):
        tls = {"enabled": True, "server_name": q.get("sni") or p.hostname,
               "utls": {"enabled": True, "fingerprint": q.get("fp") or "chrome"}}
        alpn = q.get("alpn")
        if alpn:
            tls["alpn"] = [x.strip() for x in alpn.split(",") if x.strip()]
        if q.get("security") == "reality":
            tls["reality"] = {"enabled": True, "public_key": q.get("pbk") or "",
                              "short_id": q.get("sid") or ""}
        outbound["tls"] = tls
    net = (q.get("type") or "tcp").lower()
    if net == "ws":
        outbound["transport"] = {"type": "ws", "path": q.get("path") or "/",
                                 "headers": {"Host": q.get("host") or q.get("sni") or p.hostname}}
    elif net == "grpc":
        outbound["transport"] = {"type": "grpc", "service_name": q.get("serviceName") or q.get("path") or ""}
    return outbound

def _port_open(host, port):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False

def start_singbox():
    """NODE_LINK → 本地 mixed(socks5+http) 代理。返回代理 URL 或 None"""
    global _singbox_proc
    if not NODE_LINK:
        return None
    print("⚙️ 检测到 NODE_LINK，启动本地 sing-box 代理...", flush=True)

    bin_path = shutil.which("sing-box")
    if not bin_path:
        print("❌ 设置了 NODE_LINK 但 runner 找不到 sing-box（请在 workflow 里安装）", flush=True)
        sys.exit(1)

    try:
        link = NODE_LINK.strip()
        if link.startswith("vmess://"):
            outbound = _parse_vmess(link)
        elif link.startswith("vless://"):
            outbound = _parse_vless(link)
        else:
            raise ValueError("NODE_LINK 仅支持 vmess:// 或 vless://")
    except Exception as e:
        print(f"❌ NODE_LINK 解析失败: {e}", flush=True)
        sys.exit(1)

    cfg = {
        "log": {"level": "warning"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in",
                      "listen": "127.0.0.1", "listen_port": SINGBOX_PORT}],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
    }
    cfg_path = "/tmp/sing-box-mc.json"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)

    logf = open("/tmp/sing-box-mc.log", "ab")
    _singbox_proc = subprocess.Popen([bin_path, "run", "-c", cfg_path],
                                     stdout=logf, stderr=logf)
    atexit.register(lambda: _singbox_proc and _singbox_proc.poll() is None
                    and _singbox_proc.terminate())

    for _ in range(30):
        if _singbox_proc.poll() is not None:
            print("❌ sing-box 进程退出，请检查节点", flush=True)
            sys.exit(1)
        if _port_open("127.0.0.1", SINGBOX_PORT):
            url = f"socks5://127.0.0.1:{SINGBOX_PORT}"
            print(f"✅ sing-box 就绪：{url}", flush=True)
            return url
        time.sleep(0.4)
    print("❌ sing-box 启动超时", flush=True)
    sys.exit(1)

def resolve_proxy():
    """确定最终代理：PROXY_SERVER 优先，否则尝试 NODE_LINK"""
    global REQUESTS_PROXIES
    proxy = PROXY_SERVER or start_singbox()
    if proxy:
        # requests 用 socks5h 让 DNS 也走代理
        req_proxy = proxy.replace("socks5://", "socks5h://") if proxy.startswith("socks5://") else proxy
        REQUESTS_PROXIES = {"http": req_proxy, "https": req_proxy}
        print(f"🌐 浏览器出口代理：{proxy}", flush=True)
    else:
        print("🌐 未配置代理，使用 GitHub Actions 原生出口", flush=True)
    return proxy

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
                              files={"photo": f}, timeout=30, proxies=REQUESTS_PROXIES)
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text,
                                      "parse_mode": "HTML"},
                          timeout=30, proxies=REQUESTS_PROXIES)
        log("✅ TG 通知发送成功")
    except Exception as e:
        log(f"⚠️ TG 通知失败: {e}")

def screenshot(sb, name="mc.png"):
    try:
        wait_challenge_gone(sb, timeout=6)
        sb.save_screenshot(name)
        return name
    except Exception:
        return None

# ==================== Cloudflare（综合两家之长） ====================

def is_cf_challenge(sb):
    """只在挑战【真正可见】时返回 True，隐形 Turnstile 常驻脚本不算"""
    try:
        for sel in ("#challenge-stage", "#challenge-running",
                    "#cf-challenge-running", "#cf-please-wait"):
            try:
                if sb.is_element_visible(sel):
                    return True
            except Exception:
                pass
        try:
            for f in sb.driver.find_elements(
                    By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']"):
                try:
                    if (f.is_displayed() and f.size.get("width", 0) > 100
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
        # 借自 SkyMC：页面文案特征（仅在可见 body 文本中出现才算）
        try:
            body = sb.get_text("body")[:3000]
            if "Verify you are human" in body or "Security Verification" in body:
                return True
        except Exception:
            pass
        return False
    except Exception:
        return False

def bypass_cf(sb, max_retry=4):
    if not is_cf_challenge(sb):
        return True
    log("🛡️ 检测到可见的 Cloudflare 验证，尝试通过...")
    for i in range(max_retry):
        try:
            sb.uc_gui_click_captcha()
            time.sleep(5)
            if not is_cf_challenge(sb):
                log(f"   ✅ CF 验证已通过（第 {i+1} 次）")
                return True
        except Exception as e:
            log(f"   第 {i+1} 次尝试失败: {e}")
        time.sleep(2)
    log("❌ CF 验证未通过")
    return False

def wait_challenge_gone(sb, timeout=20):
    """借自 SkyMC：轮询等待盾消失"""
    end = time.time() + timeout
    while time.time() < end:
        if not is_cf_challenge(sb):
            return True
        bypass_cf(sb, max_retry=1)
        time.sleep(1)
    return not is_cf_challenge(sb)

# ==================== 登录（5 次重试，借自 SkyMC） ====================

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
    bypass_cf(sb)

    if "login" not in sb.get_current_url().lower():
        return True

    email_sel = "input[name='email'], input[type='email'], input[name='username']"
    pass_sel = "input[type='password'], input[name='password']"

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

    submit_sel = "button[type='submit'], button#login-btn"
    for attempt in range(5):
        log(f"🔑 提交登录（第 {attempt+1} 次）...")
        try:
            sb.uc_click(submit_sel)
        except Exception:
            try:
                sb.click(submit_sel)
            except Exception:
                pass
        time.sleep(3)
        if is_cf_challenge(sb):
            bypass_cf(sb, max_retry=4)
            time.sleep(3)
        # URL 轮询确认（借自 SkyMC）
        for _ in range(10):
            if "login" not in (sb.get_current_url() or "").lower():
                log(f"✅ 登录成功 → {sb.get_current_url()}")
                return True
            time.sleep(1)

    log("❌ 5 次尝试后仍在登录页")
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
    wait_challenge_gone(sb, timeout=15)

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
            wait_challenge_gone(sb, timeout=10)
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
    log("=== MCServerHost 自动巡检启动（代理增强版）===")

    if not MC_EMAIL or not MC_PASSWORD:
        log("❌ 未配置 MC_EMAIL / MC_PASSWORD")
        return

    proxy = resolve_proxy()

    sb_kwargs = {
        "uc": True,
        "headless": False,       # 配合 Xvfb
        "ad_block": False,
        "locale_code": "en",
    }
    if proxy:
        sb_kwargs["proxy"] = proxy

    with SB(**sb_kwargs) as sb:
        try:
            sb.driver.set_page_load_timeout(45)

            if not login(sb):
                shot = screenshot(sb, "mc_error.png")
                tg_send("🔴 <b>MCServerHost 登录失败</b>\n请检查凭据 / CF 拦截 / 代理节点。", shot)
                return

            code, data = check_and_start(sb)
            now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

            if code == "ok_running":
                log("✅ 巡检完毕：全部正常，静默退出")
                return

            if code == "ok_started":
                names = "\n".join(f"🖥️ <code>{c['name']}</code>" for c, _ in data)
                tg_send(
                    f"🟢 <b>MCServerHost 已自动开机</b>\n\n{names}\n\n"
                    f"⏰ <b>执行时间：</b><code>{now_str}</code>",
                    next((f"mc_after_{c['index']}.png" for c, _ in data
                          if os.path.exists(f"mc_after_{c['index']}.png")), None))
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
                        + f"\n\n⏰ <code>{now_str}</code>", pic)
                return

            if code == "unknown":
                tg_send(f"⚪ <b>MCServerHost 状态未知</b>\n未识别到服务器卡片，可能界面改版。\n\n⏰ <code>{now_str}</code>",
                        "mc_unknown.png" if os.path.exists("mc_unknown.png") else None)

        except Exception as e:
            log(f"❌ 运行异常: {e}")
            shot = screenshot(sb, "mc_error.png")
            tg_send(f"🔴 <b>MCServerHost 脚本运行异常</b>\n\n<code>{str(e)[:500]}</code>", shot)

if __name__ == "__main__":
    main()
