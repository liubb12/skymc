#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SkyMC 自动续期脚本 v19 (休眠唤醒两步激活 + WebDriver 崩溃自动重连)
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
from urllib.parse import parse_qs, unquote, urlparse
import requests
from seleniumbase import SB
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains

EMAIL = os.environ.get("SKYMC_EMAIL") or os.environ.get("EMAIL") or ""
PASSWORD = os.environ.get("SKYMC_PASSWORD") or os.environ.get("PASSWORD") or ""
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""

SERVER_URL = os.environ.get("SERVER_URL") or "https://skymc.org/en/server/q7NPHRjwQIca"
LOGIN_URL = "https://skymc.org/en/login"
SERVER_ID = "q7NPHRjwQIca"

NODE_LINK = (os.environ.get("NODE_LINK") or "").strip()
IS_PROXY = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER = os.environ.get("PROXY_SERVER") or "socks5://127.0.0.1:7890"
SINGBOX_PORT = int(os.environ.get("SINGBOX_PORT") or "7890")
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None
_SINGBOX_PROC = None

# 全局激活防重入状态锁
_ACTIVATED_ATTEMPTED = False

EMAIL_SELECTORS = [
    "#usernameOrEmail-s1",
    'input[name="identifier"]',
    'input[id*="usernameOrEmail"]',
    'input[id*="username"]',
]
PASSWORD_SELECTORS = [
    "#password-s1",
    'input[name="password"]',
    'input[type="password"]',
    'input[id*="password"]',
]


def _b64decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    pad = (-len(data)) % 4
    return base64.b64decode(data + ("=" * pad))


def _parse_vmess(link: str) -> dict:
    raw = link[len("vmess://"):]
    obj = json.loads(_b64decode(raw).decode("utf-8"))
    host = obj.get("add") or obj.get("host") or ""
    port = int(obj.get("port") or 443)
    uuid = obj.get("id") or ""
    net = (obj.get("net") or "tcp").lower()
    tls_on = str(obj.get("tls") or "").lower() in ("tls", "reality", "1", "true")
    sni = obj.get("sni") or obj.get("host") or host
    outbound = {
        "type": "vmess",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "uuid": uuid,
        "security": obj.get("scy") or "auto",
        "alter_id": int(obj.get("aid") or 0),
    }
    if tls_on:
        outbound["tls"] = {
            "enabled": True,
            "server_name": sni,
            "insecure": False,
            "utls": {"enabled": True, "fingerprint": obj.get("fp") or "chrome"},
        }
    if net == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": obj.get("path") or "/",
            "headers": {"Host": obj.get("host") or sni or host},
        }
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": obj.get("path") or obj.get("serviceName") or "",
        }
    return outbound


def _parse_vless(link: str) -> dict:
    parsed = urlparse(link)
    uuid = unquote(parsed.username or "")
    host = parsed.hostname or ""
    port = parsed.port or 443
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    security = (q.get("security") or "none").lower()
    net = (q.get("type") or "tcp").lower()
    outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": host,
        "server_port": int(port),
        "uuid": uuid,
        "flow": q.get("flow") or "",
        "packet_encoding": "xudp",
    }
    if security in ("tls", "reality"):
        tls = {
            "enabled": True,
            "server_name": q.get("sni") or host,
            "utls": {"enabled": True, "fingerprint": q.get("fp") or "chrome"},
        }
        alpn = q.get("alpn")
        if alpn:
            tls["alpn"] = [x.strip() for x in alpn.split(",") if x.strip()]
        if security == "reality":
            tls["reality"] = {
                "enabled": True,
                "public_key": q.get("pbk") or "",
                "short_id": q.get("sid") or "",
            }
        outbound["tls"] = tls
    if net == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": q.get("path") or "/",
            "headers": {"Host": q.get("host") or q.get("sni") or host},
        }
    elif net == "grpc":
        outbound["transport"] = {
            "type": "grpc",
            "service_name": q.get("serviceName") or q.get("path") or "",
        }
    elif net == "httpupgrade":
        outbound["transport"] = {
            "type": "httpupgrade",
            "path": q.get("path") or "/",
            "headers": {"Host": q.get("host") or q.get("sni") or host},
        }
    return outbound


def build_singbox_config(node_link: str, listen_port: int) -> dict:
    link = node_link.strip()
    if link.startswith("vmess://"):
        outbound = _parse_vmess(link)
    elif link.startswith("vless://"):
        outbound = _parse_vless(link)
    else:
        raise ValueError("NODE_LINK 仅支持 vless:// 或 vmess://")
    if not outbound.get("server") or not outbound.get("uuid"):
        raise ValueError("NODE_LINK 解析失败：缺少 server 或 uuid")
    return {
        "log": {"level": "info", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": listen_port,
            }
        ],
        "outbounds": [
            outbound,
            {"type": "direct", "tag": "direct"},
        ],
    }


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def start_singbox_from_node_link():
    global IS_PROXY, PROXY_SERVER, REQUESTS_PROXIES, _SINGBOX_PROC
    if not NODE_LINK:
        return
    print("⚙️ 检测到 NODE_LINK，准备启动 sing-box 代理...", flush=True)
    scheme = NODE_LINK.split("://", 1)[0].lower() if "://" in NODE_LINK else "?"
    print(f"   协议: {scheme}://  本地端口: {SINGBOX_PORT}", flush=True)

    bin_path = shutil.which("sing-box")
    if not bin_path:
        print("❌ 已设置 NODE_LINK，但系统中找不到 sing-box", flush=True)
        sys.exit(1)

    try:
        cfg = build_singbox_config(NODE_LINK, SINGBOX_PORT)
    except Exception as e:
        print(f"❌ NODE_LINK 解析失败: {e}", flush=True)
        sys.exit(1)

    cfg_path = "/tmp/sing-box-skymc.json"
    log_path = "/tmp/sing-box-skymc.log"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    logf = open(log_path, "ab")
    _SINGBOX_PROC = subprocess.Popen(
        [bin_path, "run", "-c", cfg_path],
        stdout=logf,
        stderr=logf,
    )

    def _stop():
        if _SINGBOX_PROC and _SINGBOX_PROC.poll() is None:
            _SINGBOX_PROC.terminate()
    atexit.register(_stop)

    for _ in range(30):
        if _SINGBOX_PROC.poll() is not None:
            break
        if _port_open("127.0.0.1", SINGBOX_PORT):
            IS_PROXY = True
            PROXY_SERVER = f"socks5://127.0.0.1:{SINGBOX_PORT}"
            REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER}
            print(f"✅ sing-box 已启动，本地代理 {PROXY_SERVER}", flush=True)
            return
        time.sleep(0.4)

    print("❌ sing-box 启动失败", flush=True)
    sys.exit(1)


def send_tg(token, chat_id, message, image_path=None):
    if not token or not chat_id:
        print("⚠️ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过通知", flush=True)
        return
    message = f"【SkyMC 续期】\n{message}"
    if image_path and os.path.exists(image_path):
        try:
            with open(image_path, "rb") as f:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": message[:1024]},
                    files={"photo": f},
                    timeout=20,
                    proxies=REQUESTS_PROXIES,
                )
            if resp.status_code == 200:
                print("📨 Telegram 通知已发送（附带图片）", flush=True)
                return
        except Exception as e:
            print(f"⚠️ 带图发送异常: {e}", flush=True)
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
            proxies=REQUESTS_PROXIES,
        )
        print("📨 Telegram 通知已发送（纯文字）", flush=True)
    except Exception as e:
        print(f"❌ Telegram 发送异常: {e}", flush=True)


def get_current_ip():
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=REQUESTS_PROXIES, timeout=10)
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception:
        pass
    return "获取失败"


def challenge_visible(sb):
    try:
        src = sb.get_page_source()
        return ("Verify you are human" in src) or ("Security Verification" in src)
    except Exception:
        return False


def handle_cloudflare(sb, max_retry=3):
    if not challenge_visible(sb):
        return True
    print("🛡 检测到 Cloudflare 人机验证弹窗，开始处理...", flush=True)
    for i in range(max_retry):
        try:
            sb.uc_gui_click_captcha()
            time.sleep(5)
            if not challenge_visible(sb):
                print("   ✅ 验证已通过", flush=True)
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def wait_challenge_gone(sb, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if not challenge_visible(sb):
            return True
        handle_cloudflare(sb, max_retry=1)
        time.sleep(1)
    return not challenge_visible(sb)


def safe_screenshot(sb, path):
    try:
        wait_challenge_gone(sb, timeout=8)
        time.sleep(1)
        sb.save_screenshot(path)
        print(f"📸 已保存截图: {path}", flush=True)
    except Exception as e:
        print(f"📸 截图保存异常: {e}", flush=True)
    return path


def fill_field(sb, selectors, value, field_name, timeout=20):
    print(f"   填写 {field_name} ...", flush=True)
    end = time.time() + timeout
    while time.time() < end:
        for sel in selectors:
            try:
                sb.wait_for_element_visible(sel, timeout=2)
                sb.clear(sel)
                sb.type(sel, value)
                print(f"   ✅ {field_name} 已填写", flush=True)
                return True
            except Exception:
                continue
        time.sleep(1)
    return False


def click_login(sb):
    for sel in ['button:contains("Login")', 'button[type="submit"]', 'button:contains("Sign in")']:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                return True
        except Exception:
            continue
    return False


def login(sb, email, password):
    print("🌐 打开登录页面...", flush=True)
    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
    except Exception:
        sb.open(LOGIN_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(3)

    handle_cloudflare(sb)
    time.sleep(1)

    print("📧 填写邮箱/用户名...", flush=True)
    if not fill_field(sb, EMAIL_SELECTORS, email, "邮箱", timeout=25):
        safe_screenshot(sb, "login_failed.png")
        return False

    print("🔑 填写密码...", flush=True)
    if not fill_field(sb, PASSWORD_SELECTORS, password, "密码", timeout=15):
        safe_screenshot(sb, "login_failed.png")
        return False

    time.sleep(1)
    handle_cloudflare(sb)

    for attempt in range(5):
        print(f"🔑 点击登录按钮...(第 {attempt + 1} 次)", flush=True)
        click_login(sb)
        time.sleep(3)
        if challenge_visible(sb):
            handle_cloudflare(sb, max_retry=4)
            time.sleep(3)
        for _ in range(10):
            url = (sb.get_current_url() or "").lower()
            if "login" not in url:
                print(f"✅ 登录成功！当前页面: {sb.get_current_url()}", flush=True)
                return True
            time.sleep(1)
        time.sleep(2)

    return False


def is_activate_page(sb):
    """检测是否处于激活/重新激活页面（COAL 套餐选择 + Start Server）。
    注意：只匹配激活页特有的元素，避免在普通面板上误判。
    """
    try:
        url = (sb.get_current_url() or "").lower()
        if "reactivate" in url or "activate" in url:
            return True
        body = sb.get_text("body")
        if "Activate Your Server" in body:
            return True
        if "COAL" in body and "Free" in body and ("Plan" in body or "GB" in body):
            return True
        if "Start Server" in body and ("COAL" in body or "Location" in body or "Summary" in body):
            return True
    except Exception:
        pass
    return False


def handle_reactivate_flow(sb):
    """
    两步完成激活流程：① 选 COAL → ② 点 Start Server。
    Location 默认预选（France），不需要单独点击。
    具备全局防重复执行锁，保护 ChromeDriver。
    如果 Start Server 未成功点击，重置锁并返回 False 以便后续重试。
    如果页面上没有激活页元素（COAL/Start Server），自动跳过。
    """
    global _ACTIVATED_ATTEMPTED
    if _ACTIVATED_ATTEMPTED:
        return False

    if not is_activate_page(sb):
        return False

    _ACTIVATED_ATTEMPTED = True
    print("\n⚡ 检测到处于 [Activate/Reactivate] 激活选配页面，开始执行两步激活流程...", flush=True)
    safe_screenshot(sb, "before_activate.png")
    driver = sb.driver

    # ─────────────────────────────────────────────────────
    # 步骤 1：点击选中 COAL Free 卡片
    # ─────────────────────────────────────────────────────
    print("👉 步骤 ①：点击选中 [COAL Free] 免费套餐卡片...", flush=True)
    coal_clicked = False
    # 用 JS 找到最小的包含 COAL + Free 文本的可点击元素，避免命中外层大容器
    try:
        coal_clicked = driver.execute_script("""
            var bestEl = null, bestSize = Infinity;
            var all = document.querySelectorAll('div, button, a, span, label, [role="button"], [role="option"]');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                var t = (el.innerText || '').trim();
                if (t.indexOf('COAL') < 0) continue;
                if (t.toLowerCase().indexOf('free') < 0) continue;
                if (t.length > 200) continue;              /* 跳过外层容器 */
                var r = el.getBoundingClientRect();
                if (r.width < 30 || r.height < 15) continue;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                var size = r.width * r.height;
                if (size < bestSize) { bestSize = size; bestEl = el; }
            }
            if (bestEl) {
                bestEl.scrollIntoView({block: 'center'});
                bestEl.click();
                return true;
            }
            return false;
        """)
        if coal_clicked:
            print("   ✅ 已通过 JS 点击 COAL Free 卡片", flush=True)
    except Exception as e:
        print(f"   JS 点击 COAL 异常: {e}", flush=True)

    # 备用：ActionChains + XPath
    if not coal_clicked:
        for xpath in [
            "//*[contains(text(), 'COAL')]/ancestor::div[contains(@class, 'border') or contains(@class, 'cursor')][1]",
            "//*[contains(text(), 'COAL')]/..",
        ]:
            try:
                elems = driver.find_elements(By.XPATH, xpath)
                for el in elems:
                    if el.is_displayed() and 15 < el.size.get("height", 0) < 500:
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                        time.sleep(0.5)
                        ActionChains(driver).move_to_element(el).pause(0.2).click().perform()
                        print(f"   ✅ 已通过 ActionChains 点击: {xpath}", flush=True)
                        coal_clicked = True
                        break
                if coal_clicked:
                    break
            except Exception:
                continue

    time.sleep(5)
    safe_screenshot(sb, "after_coal_selected.png")

    # ─────────────────────────────────────────────────────
    # 步骤 2：点击 Start Server 按钮
    # ─────────────────────────────────────────────────────
    print("👉 步骤 ②：点击页面最底部的 [Start Server] 按钮...", flush=True)
    start_btn_clicked = False

    # 先用 JS 找到包含 "Start Server" 文本的最小可点击元素（button / div / a）
    try:
        start_btn_clicked = driver.execute_script("""
            var bestEl = null, bestSize = Infinity;
            var all = document.querySelectorAll('button, a, div, [role="button"]');
            for (var i = 0; i < all.length; i++) {
                var el = all[i];
                var t = (el.innerText || '').trim();
                if (t.indexOf('Start Server') < 0) continue;
                if (t.length > 50) continue;            /* "Start Server" 文本很短 */
                var r = el.getBoundingClientRect();
                if (r.width < 40 || r.height < 20) continue;
                var st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                var size = r.width * r.height;
                if (size < bestSize) { bestSize = size; bestEl = el; }
            }
            if (bestEl) {
                bestEl.scrollIntoView({block: 'center'});
                bestEl.click();
                return true;
            }
            return false;
        """)
        if start_btn_clicked:
            print("   ✅ 已通过 JS 点击 Start Server 按钮", flush=True)
    except Exception as e:
        print(f"   JS 点击 Start Server 异常: {e}", flush=True)

    # 备用：ActionChains + XPath
    if not start_btn_clicked:
        for btn_xpath in [
            "//button[contains(., 'Start Server')]",
            "//*[@role='button' and contains(., 'Start Server')]",
            "//button[contains(@class, 'bg-cyan') or contains(@class, 'bg-teal')]",
            "//button[contains(., 'Start')]",
        ]:
            try:
                btns = driver.find_elements(By.XPATH, btn_xpath)
                for btn in btns:
                    if btn.is_displayed():
                        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.5)
                        ActionChains(driver).move_to_element(btn).pause(0.2).click().perform()
                        print(f"   ✅ 已通过 ActionChains 点击: {btn.text or btn_xpath}", flush=True)
                        start_btn_clicked = True
                        break
                if start_btn_clicked:
                    break
            except Exception:
                continue

    if not start_btn_clicked:
        print("⚠️ 未成功点击 Start Server 按钮，重置激活锁以便后续重试", flush=True)
        _ACTIVATED_ATTEMPTED = False
        safe_screenshot(sb, "activate_failed.png")
        return False

    print("⏳ 等待激活完成与实例初始化（给予 20 秒）...", flush=True)
    time.sleep(20)
    handle_cloudflare(sb)
    safe_screenshot(sb, "after_activate_flow.png")

    # 激活完成后用 uc_open_with_reconnect 回到主控制台（防止浏览器崩溃）
    try:
        sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=6)
        time.sleep(3)
        handle_cloudflare(sb)
    except Exception as e:
        print(f"重新访问控制台异常: {e}", flush=True)

    return True


def driver_is_alive(sb):
    """检查 WebDriver 连接是否存活"""
    try:
        sb.driver.execute_script("return 1;")
        return True
    except Exception:
        return False


def reconnect_driver(sb, url=None):
    """尝试通过 uc_open_with_reconnect 重连浏览器"""
    target = url or SERVER_URL
    print(f"🔧 尝试重连浏览器到 {target} ...", flush=True)
    try:
        sb.uc_open_with_reconnect(target, reconnect_time=6)
        time.sleep(3)
        handle_cloudflare(sb)
        if driver_is_alive(sb):
            print("   ✅ 浏览器重连成功", flush=True)
            return True
    except Exception as e:
        print(f"   ❌ 浏览器重连失败: {e}", flush=True)
    return False


def open_server_panel(sb):
    print("📄 进入服务器面板...", flush=True)
    try:
        sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=5)
    except Exception:
        sb.open(SERVER_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(5)
    handle_cloudflare(sb)
    wait_challenge_gone(sb, timeout=15)

    if is_activate_page(sb):
        handle_reactivate_flow(sb)


def read_panel_info(sb):
    if is_activate_page(sb):
        handle_reactivate_flow(sb)

    script = r"""
        var body = (document.body && document.body.innerText) ? document.body.innerText : '';
        var status = 'unknown';
        if (/\bOnline\b/i.test(body) || /在线/.test(body)) status = 'Online';
        else if (/\bStarting\b/i.test(body) || /启动中/.test(body)) status = 'Starting';
        else if (/\bStopping\b/i.test(body) || /关闭中/.test(body)) status = 'Stopping';
        else if (/\bSleeping\b/i.test(body) || /\bHibernating\b/i.test(body) || /休眠/.test(body) || /\bSuspended\b/i.test(body)) status = 'Sleeping';
        else if (/\bOffline\b/i.test(body) || /\bStopped\b/i.test(body) || /离线/.test(body) || /已停止/.test(body)) status = 'Offline';

        var remaining = null;
        var exp = body.match(/Expires\s+in\s+(\d+)\s*h(?:\s*(\d+)\s*m)?/i);
        if (exp) {
            var h = parseInt(exp[1], 10) || 0;
            var m = parseInt(exp[2] || '0', 10) || 0;
            remaining = (h * 60 + m) + 'm';
        } else {
            exp = body.match(/Expires\s+in\s+(\d+)\s*m/i);
            if (exp) {
                remaining = exp[1] + 'm';
            }
        }
        if (!remaining) {
            var matches = body.match(/\b(\d{1,3}:\d{2})\b/g) || [];
            if (matches.length) remaining = matches[0];
        }

        var hasStart = false, hasStop = false, hasRestart = false, hasRenew = false, hasExpires = false;
        var buttons = [];
        var all = document.querySelectorAll('button, a, [role="button"], div, span');
        for (var i = 0; i < all.length; i++) {
            var b = all[i];
            var text = (b.innerText || b.textContent || '').replace(/\s+/g, ' ').trim();
            if (!text || text.length > 80) continue;
            var aria = (b.getAttribute('aria-label') || '') + ' ' + (b.getAttribute('title') || '');
            var html = (b.innerHTML || '').toLowerCase();
            var blob = (text + ' ' + aria).toLowerCase();
            var visible = b.offsetParent !== null;
            if (!visible) continue;
            if (b.tagName === 'BUTTON' || b.getAttribute('role') === 'button') {
                buttons.push({text: text, disabled: !!b.disabled, visible: visible});
            }
            if (b.disabled) continue;
            if (blob.indexOf('start') >= 0 || blob.indexOf('启动') >= 0 || html.indexOf('fa-play') >= 0) hasStart = true;
            if (blob.indexOf('stop') >= 0 || blob.indexOf('停止') >= 0 || blob.indexOf('关机') >= 0) hasStop = true;
            if (blob.indexOf('restart') >= 0 || blob.indexOf('重启') >= 0) hasRestart = true;
            if (blob.indexOf('renew') >= 0 || blob.indexOf('续期') >= 0) hasRenew = true;
            if (blob.indexOf('expires in') >= 0 || blob.indexOf('expire') >= 0) hasExpires = true;
        }
        return JSON.stringify({
            status: status,
            remaining: remaining,
            hasStart: hasStart,
            hasStop: hasStop,
            hasRestart: hasRestart,
            hasRenew: hasRenew,
            hasExpires: hasExpires,
            buttons: buttons
        });
    """
    try:
        raw = sb.driver.execute_script(script)
        info = json.loads(raw) if raw else {}
    except Exception as e:
        print(f"   读取面板信息失败: {e}", flush=True)
        info = {}
    status = info.get("status") or "unknown"
    remaining = info.get("remaining")
    print(f"   面板状态: {status}  剩余时间: {remaining or '未读到'}  "
          f"Start={info.get('hasStart')} Stop={info.get('hasStop')} "
          f"Expires={info.get('hasExpires')} Renew={info.get('hasRenew')}", flush=True)
    return info


def format_remaining(val):
    if not val:
        return "未读到"
    s = str(val).strip()
    if s.endswith("m") and s[:-1].isdigit():
        mins = int(s[:-1])
        return f"{mins}分钟（Expires in {mins}m）"
    parts = s.split(":")
    try:
        if len(parts) == 2:
            m, sec = int(parts[0]), int(parts[1])
            return f"{s}（{m}分钟{sec}秒）"
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
            return f"{s}（{h}小时{m}分{sec}秒）"
    except Exception:
        pass
    return s


def remaining_to_seconds(val):
    if not val:
        return None
    s = str(val).strip().lower()
    try:
        if s.endswith("m") and s[:-1].isdigit():
            return int(s[:-1]) * 60
        parts = s.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except Exception:
        return None
    return None


def click_named_button(sb, names):
    for name in names:
        sel = f'button:contains("{name}")'
        try:
            if sb.is_element_visible(sel) and sb.is_element_enabled(sel):
                sb.uc_click(sel)
                print(f"   已点击按钮: {name}", flush=True)
                return True
        except Exception:
            continue
    try:
        result = sb.driver.execute_script(
            """
            var names = arguments[0];
            var buttons = document.querySelectorAll('button');
            for (var i = 0; i < buttons.length; i++) {
                var b = buttons[i];
                if (b.disabled || b.offsetParent === null) continue;
                var blob = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '') + ' ' + (b.innerHTML || '')).toLowerCase();
                for (var j = 0; j < names.length; j++) {
                    if (blob.indexOf(names[j].toLowerCase()) >= 0) {
                        b.click();
                        return names[j];
                    }
                }
            }
            return null;
            """,
            names,
        )
        if result:
            print(f"   已通过 JS 点击按钮: {result}", flush=True)
            return True
    except Exception as e:
        print(f"   JS 点击按钮失败: {e}", flush=True)
    return False


def wait_until_online(sb, timeout_sec=180, action_label="启动/重启"):
    print(f"⏳ 等待服务器进入 Online（最多 {timeout_sec} 秒）...", flush=True)
    end = time.time() + timeout_sec
    last_status = ""
    while time.time() < end:
        # 检查浏览器是否存活，崩溃则尝试重连
        if not driver_is_alive(sb):
            print("⚠️ 等待中 WebDriver 断开，尝试重连...", flush=True)
            if not reconnect_driver(sb):
                print("❌ 浏览器重连失败，退出等待", flush=True)
                break
            continue
        handle_cloudflare(sb)
        if is_activate_page(sb):
            handle_reactivate_flow(sb)

        info = read_panel_info(sb)
        st = (info.get("status") or "").lower()
        last_status = st or last_status
        if st == "online":
            time.sleep(3)
            info2 = read_panel_info(sb)
            if (info2.get("status") or "").lower() == "online":
                print(f"✅ {action_label}成功，服务器已 Online", flush=True)
                return True, info2
        if st == "starting":
            print("   仍在 Starting，继续等待...", flush=True)
        elif st in ("offline", "stopped"):
            print("   当前 Offline/Stopped，继续等待...", flush=True)
        else:
            print(f"   当前状态: {st or 'unknown'}，继续等待...", flush=True)
        time.sleep(5)
    print(f"⚠️ 等待超时，最后状态仍为: {last_status or 'unknown'}", flush=True)
    return False, read_panel_info(sb)


def ensure_server_running(sb, info):
    global _ACTIVATED_ATTEMPTED
    status = (info.get("status") or "").lower()
    has_restart = info.get("hasRestart")

    if status == "sleeping":
        print("💤 服务器处于休眠状态，尝试跳转到激活页...", flush=True)
        _ACTIVATED_ATTEMPTED = False
        activated = False
        # 先打开面板，看是否自动跳转到 reactivate 页
        try:
            sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=5)
        except Exception:
            sb.open(SERVER_URL)
        sb.wait_for_ready_state_complete()
        time.sleep(3)
        handle_cloudflare(sb)
        wait_challenge_gone(sb, timeout=15)
        if is_activate_page(sb):
            activated = handle_reactivate_flow(sb)
        # 如果没检测到激活页，尝试手动拼 /reactivate URL
        if not activated:
            try:
                reactivate_url = SERVER_URL.rstrip("/") + "/reactivate"
                sb.uc_open_with_reconnect(reactivate_url, reconnect_time=5)
            except Exception:
                sb.open(reactivate_url)
            sb.wait_for_ready_state_complete()
            time.sleep(3)
            handle_cloudflare(sb)
            if is_activate_page(sb):
                activated = handle_reactivate_flow(sb)
        # 如果仍然没有激活页，自动跳过，直接尝试点 Start
        if not activated:
            print("   ⚠️ 未检测到激活页面，自动跳过激活流程，尝试直接启动...", flush=True)
            click_named_button(sb, ["Start", "启动", "Start Server"])
            time.sleep(5)
        ok, _ = wait_until_online(sb, timeout_sec=180, action_label="休眠唤醒")
        return (True, "休眠唤醒成功，已进入 Online") if ok else (False, "休眠唤醒超时")

    if status == "online":
        print("   服务器已 Online，无需启动", flush=True)
        return True, "已在运行"

    if status == "starting":
        print("🔌 服务器正在 Starting，等待启动...", flush=True)
        ok, _ = wait_until_online(sb, timeout_sec=180, action_label="启动")
        return (True, "等待 Starting 完成，已进入 Online") if ok else (False, "Starting 超时")

    print(f"🔌 服务器状态为 {status or 'unknown'}，正在点击绿色 Start 开机按钮...", flush=True)
    clicked = click_named_button(sb, ["Start", "启动"])
    if not clicked:
        try:
            sb.driver.execute_script(
                """
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var b = btns[i];
                    if ((b.className || '').indexOf('green') >= 0 || (b.className || '').indexOf('success') >= 0 || (b.innerText || '').indexOf('Start') >= 0) {
                        b.click(); return true;
                    }
                }
                return false;
                """
            )
            clicked = True
        except Exception:
            pass

    if not clicked and has_restart:
        clicked = click_named_button(sb, ["Restart", "重启"])

    time.sleep(5)
    handle_cloudflare(sb)
    ok, _ = wait_until_online(sb, timeout_sec=180, action_label="启动")
    return (True, "已点击启动，服务器已 Online") if ok else (False, "等待启动进入 Online 超时")


def _is_visible_box(rect):
    return rect and rect.get("w", 0) > 5 and rect.get("h", 0) > 5


def locate_expires_button(sb):
    try:
        info = sb.driver.execute_script(
            r"""
            var best = null;
            var nodes = document.querySelectorAll('button, a, [role="button"]');
            for (var i = 0; i < nodes.length; i++) {
                var n = nodes[i];
                var t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
                if (!/expires\s+in\s+\d+/i.test(t)) continue;
                var r = n.getBoundingClientRect();
                if (r.width < 5 || r.height < 5) continue;
                var st = window.getComputedStyle(n);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
                var score = r.bottom + (r.left < 400 ? 1000 : 0);
                var item = {
                    text: t,
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    w: r.width,
                    h: r.height,
                    score: score
                };
                if (!best || item.score > best.score) best = item;
            }
            return best ? JSON.stringify(best) : null;
            """
        )
        return json.loads(info) if info else None
    except Exception:
        return None


def locate_renew_option(sb):
    try:
        info = sb.driver.execute_script(
            r"""
            var best = null;
            var nodes = document.querySelectorAll('button, a, [role="menuitem"], [role="option"], li, div, span');
            for (var i = 0; i < nodes.length; i++) {
                var n = nodes[i];
                var t = (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
                if (!(/^renew$/i.test(t) || t === '续期')) continue;
                if (/expires/i.test(t)) continue;
                var r = n.getBoundingClientRect();
                if (r.width < 3 || r.height < 3) continue;
                var st = window.getComputedStyle(n);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                best = {
                    text: t,
                    x: r.left + r.width / 2,
                    y: r.top + r.height / 2,
                    w: r.width,
                    h: r.height
                };
                break;
            }
            return best ? JSON.stringify(best) : null;
            """
        )
        return json.loads(info) if info else None
    except Exception:
        return None


def renew_visible_in_dom(sb):
    return locate_renew_option(sb) is not None


def _cdp_click_xy(sb, x, y):
    try:
        driver = sb.driver
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0})
        time.sleep(0.05)
        for etype in ("mousePressed", "mouseReleased"):
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {"type": etype, "x": x, "y": y, "button": "left", "buttons": 1 if etype == "mousePressed" else 0, "clickCount": 1},
            )
            time.sleep(0.05)
        return True
    except Exception:
        return False


def _action_chains_click_expires(sb):
    try:
        candidates = sb.driver.find_elements(By.XPATH, "//button[contains(., 'Expires in')] | //*[@role='button' and contains(., 'Expires in')]")
        target = None
        for el in candidates:
            if el.is_displayed() and el.size.get("width", 0) > 5:
                target = el
                break
        if not target:
            return False

        sb.driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", target)
        time.sleep(0.3)
        ActionChains(sb.driver).move_to_element(target).pause(0.3).click(target).perform()
        time.sleep(1.0)
        return renew_visible_in_dom(sb)
    except Exception:
        return False


def open_expires_menu(sb):
    if _action_chains_click_expires(sb):
        return True
    info = locate_expires_button(sb)
    if info and _is_visible_box(info):
        _cdp_click_xy(sb, float(info["x"]), float(info["y"]))
        time.sleep(1.2)
        if renew_visible_in_dom(sb):
            return True
    return False


def click_renew_option(sb):
    info = locate_renew_option(sb)
    if info and _is_visible_box(info):
        if _cdp_click_xy(sb, float(info["x"]), float(info["y"])):
            time.sleep(0.8)
            return True

    for sel in ['button:contains("Renew")', '//button[normalize-space()="Renew"]', '//*[@role="menuitem" and contains(., "Renew")]']:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                return True
        except Exception:
            continue
    return False


def click_renew(sb):
    print("🔍 开始续期：Expires → 等待 Renew → 点击 Renew...", flush=True)
    for attempt in range(1, 4):
        print(f"   第 {attempt} 次尝试打开 Expires 菜单...", flush=True)
        open_expires_menu(sb)

        for _ in range(15):
            if renew_visible_in_dom(sb):
                break
            time.sleep(0.5)

        if not renew_visible_in_dom(sb):
            try:
                sb.driver.execute_script("document.body.click();")
            except Exception:
                pass
            time.sleep(1)
            continue

        print("✅ 已检测到 Renew，准备点击...", flush=True)
        if click_renew_option(sb):
            time.sleep(3)
            if challenge_visible(sb):
                handle_cloudflare(sb, max_retry=4)
                time.sleep(3)
                if renew_visible_in_dom(sb):
                    click_renew_option(sb)
            wait_challenge_gone(sb, timeout=20)
            return True
        time.sleep(1)

    safe_screenshot(sb, "renew_not_found.png")
    return False


def main():
    if not EMAIL or not PASSWORD:
        print("❌ 请设置环境变量 SKYMC_EMAIL 和 SKYMC_PASSWORD", flush=True)
        sys.exit(1)

    print("🚀 启动 SkyMC 自动续期脚本 v19", flush=True)
    print(f"目标服务器: {SERVER_URL}", flush=True)

    start_singbox_from_node_link()
    current_ip = get_current_ip()
    print(f"🎯 当前出口 IP: {current_ip}", flush=True)

    sb_kwargs = {"uc": True, "headless": False, "locale_code": "en"}
    if IS_PROXY:
        sb_kwargs["proxy"] = PROXY_SERVER
        print(f"⚙️ 已启用代理: {PROXY_SERVER}", flush=True)

    with SB(**sb_kwargs) as sb:
        if not login(sb, EMAIL, PASSWORD):
            msg = f"❌ 登录失败\nIP: {current_ip}"
            print(msg, flush=True)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg, image_path="login_failed.png")
            return

        # 1. 打开服务器面板（包含激活穿透）
        open_server_panel(sb)

        # 激活流程可能导致浏览器崩溃，检查并重连
        if not driver_is_alive(sb):
            print("⚠️ 激活后 WebDriver 连接已断开，尝试重连...", flush=True)
            if not reconnect_driver(sb):
                safe_screenshot(sb, "driver_crashed.png")
                msg = f"❌ 浏览器崩溃，无法恢复\n服务器: {SERVER_ID}\nIP: {current_ip}"
                print(msg, flush=True)
                send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg, image_path="after_activate_flow.png")
                return

        before = read_panel_info(sb)
        before_time = before.get("remaining")
        print(f"⏱ 续期前剩余时间: {format_remaining(before_time)}", flush=True)

        # 2. 保证 Online 状态（未启动则自动 Start，休眠则自动激活）
        started_ok, start_msg = ensure_server_running(sb, before)
        after_start = read_panel_info(sb)

        if not started_ok:
            safe_screenshot(sb, "final_result.png")
            msg = (
                f"❌ 续期跳过：服务器未 Online\n"
                f"服务器: {SERVER_ID}\n"
                f"当前状态: {(after_start.get('status') or 'unknown')}\n"
                f"启动操作: {start_msg}\n"
                f"续期前时间: {format_remaining(before_time)}\n"
                f"IP: {current_ip}"
            )
            print(msg, flush=True)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg, image_path="final_result.png")
            return

        # 3. 执行续期
        print("\n📄 开始续期流程（服务器已 Online）...", flush=True)
        renew_ok = click_renew(sb)

        print("⏳ 等待续期结果刷新...", flush=True)
        time.sleep(5)
        handle_cloudflare(sb)
        wait_challenge_gone(sb, timeout=15)
        after = read_panel_info(sb)
        after_time = after.get("remaining")
        print(f"⏱ 续期后剩余时间: {format_remaining(after_time)}", flush=True)

        safe_screenshot(sb, "final_result.png")

        before_sec = remaining_to_seconds(before_time)
        after_sec = remaining_to_seconds(after_time)
        time_note = "无法对比（有一侧未读到时间）"
        if before_sec is not None and after_sec is not None:
            delta = after_sec - before_sec
            if delta > 30:
                time_note = f"倒计时已增加约 {delta} 秒，续期生效"
            elif delta >= -5:
                time_note = "倒计时变化很小，可能刚续过或尚未刷新"
            else:
                time_note = "倒计时未增加，请人工核对截图"

        status_now = after.get("status") or after_start.get("status") or "unknown"
        lines = [
            f"{'✅' if renew_ok else '❌'} 续期{'已执行' if renew_ok else '失败'}",
            f"服务器: {SERVER_ID}",
            f"当前状态: {status_now}",
            f"启动操作: {start_msg}",
            f"续期前时间: {format_remaining(before_time)}",
            f"续期后时间: {format_remaining(after_time)}",
            f"结论: {time_note}",
            f"IP: {current_ip}",
        ]
        msg = "\n".join(lines)
        print("\n" + msg, flush=True)
        img = "final_result.png" if os.path.exists("final_result.png") else None
        if not renew_ok and os.path.exists("renew_not_found.png"):
            img = "renew_not_found.png"
        send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg, image_path=img)

    print("🏁 脚本执行完毕", flush=True)


if __name__ == "__main__":
    main()
