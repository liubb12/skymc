#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Gaming4Free 自动续期巡检 (现场图像诊断 + 视频右上角精准结算终极版)
# ============================================================
import atexit
import base64
import html
import json
import os
import re
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
from seleniumbase import Driver

BASE_URL = "https://control.gaming4free.net"
CONSOLE_URL = os.environ.get(
    "G4F_SERVER_URL", 
    "https://control.gaming4free.net/server/c2d0a619/console"
).strip()

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
G4F_COOKIE = os.environ.get("G4F_COOKIE", "").strip()

NODE_LINK = (os.environ.get("G4F_NODE_LINK") or os.environ.get("NODE_LINK") or "").strip()
PROXY_SERVER = (os.environ.get("G4F_PROXY_SERVER") or os.environ.get("PROXY_SERVER") or "").strip()
SINGBOX_PORT = int(os.environ.get("SINGBOX_PORT") or "7890")
IS_PROXY = False
REQUESTS_PROXIES = None
_SINGBOX_PROC = None


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


def _parse_hy2(link: str) -> dict:
    parsed = urlparse(link)
    auth = unquote(parsed.username or parsed.password or "")
    host = parsed.hostname or ""
    port = int(parsed.port or 443)
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    sni = q.get("sni") or host
    insecure = q.get("insecure") in ("1", "true")

    outbound = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "password": auth,
        "tls": {
            "enabled": True,
            "server_name": sni,
            "insecure": insecure,
        }
    }
    return outbound


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def setup_network_proxy():
    global IS_PROXY, PROXY_SERVER, REQUESTS_PROXIES, _SINGBOX_PROC
    raw = (NODE_LINK or PROXY_SERVER).strip()
    if not raw:
        return

    if raw.startswith(("socks5://", "socks://", "http://", "https://")):
        IS_PROXY = True
        PROXY_SERVER = raw
        REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER}
        print(f"✅ 直接使用外接代理: {PROXY_SERVER}", flush=True)
        return

    if raw.startswith(("vless://", "vmess://", "hysteria2://", "hy2://")):
        print("⚙️ 检测到节点链接，准备启动 sing-box 本地代理...", flush=True)
        bin_path = shutil.which("sing-box")
        if not bin_path:
            print("❌ 系统中找不到 sing-box 可执行程序", flush=True)
            sys.exit(1)

        try:
            if raw.startswith("vmess://"):
                outbound = _parse_vmess(raw)
            elif raw.startswith(("hysteria2://", "hy2://")):
                outbound = _parse_hy2(raw)
            else:
                outbound = _parse_vless(raw)
        except Exception as e:
            print(f"❌ 节点解析失败: {e}", flush=True)
            sys.exit(1)

        cfg = {
            "log": {"level": "info", "timestamp": True},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "mixed-in",
                    "listen": "127.0.0.1",
                    "listen_port": SINGBOX_PORT,
                }
            ],
            "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        }

        cfg_path = "/tmp/sing-box-g4f.json"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

        _SINGBOX_PROC = subprocess.Popen([bin_path, "run", "-c", cfg_path])
        atexit.register(lambda: _SINGBOX_PROC.terminate() if _SINGBOX_PROC and _SINGBOX_PROC.poll() is None else None)

        for _ in range(25):
            if _port_open("127.0.0.1", SINGBOX_PORT):
                IS_PROXY = True
                PROXY_SERVER = f"socks5://127.0.0.1:{SINGBOX_PORT}"
                REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER}
                print(f"✅ sing-box 启动成功，本地代理: {PROXY_SERVER}", flush=True)
                return
            time.sleep(0.4)

        print("❌ sing-box 启动超时", flush=True)
        sys.exit(1)

    print(f"❌ 无法识别的代理格式: {raw[:15]}...", flush=True)
    sys.exit(1)


def get_current_ip():
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=REQUESTS_PROXIES, timeout=10)
        if resp.status_code == 200:
            return resp.text.strip()
    except Exception:
        pass
    return "获取失败"


def tg_send(text: str, photo_path: str = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过通知。", flush=True)
        return
    try:
        if photo_path and os.path.exists(photo_path) and os.path.getsize(photo_path) > 1000:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as f:
                requests.post(
                    url,
                    data={"chat_id": TG_CHAT_ID, "caption": text, "parse_mode": "HTML"},
                    files={"photo": f},
                    proxies=REQUESTS_PROXIES,
                    timeout=30,
                )
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            requests.post(
                url,
                data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                proxies=REQUESTS_PROXIES,
                timeout=30,
            )
        print("  ✅ TG 通知发送成功", flush=True)
    except Exception as e:
        print(f"  ⚠️ TG 通知异常: {e}", flush=True)


def capture_screenshot_smart(driver, save_path="g4f_result.png"):
    try:
        driver.save_screenshot(save_path)
        if os.path.exists(save_path) and os.path.getsize(save_path) > 15000:
            return True
    except Exception:
        pass

    try:
        subprocess.run(["scrot", "-u", save_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception:
        pass
    return True


def non_blocking_navigate(driver, url, wait_seconds=5):
    try:
        driver.execute_script(f"window.location.href = '{url}';")
    except Exception:
        pass
    time.sleep(wait_seconds)
    try:
        driver.execute_script("window.stop();")
    except Exception:
        pass


def force_scroll_and_reveal_session_card(driver):
    driver.execute_script("""
        var divs = Array.from(document.querySelectorAll('*'));
        for (var d of divs) {
            if ((d.innerText || '').includes('Active session')) {
                d.scrollIntoView({behavior: 'instant', block: 'center'});
                break;
            }
        }
        var scrollables = document.querySelectorAll('aside, nav, [class*="sidebar"], [class*="navigation"], div');
        for (var s of scrollables) {
            if (s.scrollHeight > s.clientHeight && s.clientWidth < 400 && s.clientWidth > 100) {
                s.scrollTop = s.scrollHeight;
            }
        }
        window.scrollTo(0, document.body.scrollHeight);
    """)
    time.sleep(1)


def is_real_turnstile_modal(driver):
    try:
        body_text = driver.execute_script("return (document.body ? document.body.innerText : '');")
        if "Verify you’re human to continue" in body_text or "Verify you're human" in body_text:
            token = driver.execute_script("var el = document.querySelector('[name=\"cf-turnstile-response\"]'); return el ? el.value : '';")
            if not token or len(token) < 25:
                return True
    except Exception:
        pass
    return False


def solve_turnstile_if_present(driver, max_wait=20):
    if not is_real_turnstile_modal(driver):
        return True

    print("  🛡️ 命中【Cloudflare Turnstile 验证弹窗】，开始破解...", flush=True)
    end_time = time.time() + max_wait
    while time.time() < end_time:
        try:
            token = driver.execute_script("var el = document.querySelector('[name=\"cf-turnstile-response\"]'); return el ? el.value : '';")
            if token and len(token) > 25:
                print("  🎉 截获有效 Turnstile Token！", flush=True)
                return True
        except Exception:
            pass

        try:
            driver.uc_gui_click_captcha()
        except Exception:
            pass

        if not is_real_turnstile_modal(driver):
            return True
        time.sleep(2)

    return False


def kill_video_close_button_precision(driver):
    """
    定位视频广告容器并点击右上角圆圈叉号
    """
    clicked = driver.execute_script("""
        function fireClick(elem) {
            if (!elem) return;
            var rect = elem.getBoundingClientRect();
            ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(evt) {
                elem.dispatchEvent(new MouseEvent(evt, {
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    clientX: rect.left + rect.width / 2,
                    clientY: rect.top + rect.height / 2
                }));
            });
            if (elem.click) elem.click();
        }

        // 1. 扫描右下角视频卡片区域内的关闭元素
        var vids = document.querySelectorAll('video');
        for (var v of vids) {
            var rect = v.getBoundingClientRect();
            // 在视频右上角区域点位刺探
            var cornerX = rect.right - 12;
            var cornerY = rect.top + 12;
            var cornerEls = document.elementsFromPoint(cornerX, cornerY) || [];
            for (var el of cornerEls) {
                if (el !== v) {
                    fireClick(el);
                    return true;
                }
            }

            // 检查视频父容器中的按钮
            var p = v.parentElement;
            for (var i = 0; i < 4 && p; i++) {
                var btns = p.querySelectorAll('button, svg, [role="button"], div, span');
                for (var b of btns) {
                    var txt = (b.innerText || '').trim();
                    var cls = (b.className || '').toString().toLowerCase();
                    if (txt === '✕' || txt === '×' || txt === 'x' || cls.includes('close') || cls.includes('dismiss')) {
                        fireClick(b);
                        return true;
                    }
                }
                p = p.parentElement;
            }
        }

        // 2. 全局通用关闭元素
        var allBtns = document.querySelectorAll('button, svg, [role="button"], div, span');
        for (var b of allBtns) {
            var txt = (b.innerText || '').trim();
            var aria = (b.getAttribute('aria-label') || '').toLowerCase();
            if (txt === '✕' || txt === '×' || txt.toLowerCase() === 'x' || aria.includes('close')) {
                if (b.offsetWidth > 0 && b.offsetHeight > 0) {
                    fireClick(b);
                    return true;
                }
            }
        }
        return false;
    """)
    return clicked


def handle_video_and_ad_lifecycle(driver, total_wait=50):
    """
    视频播放全流程监控
    """
    print(f"⏳ 正在监听视频广告与浮层（最长等待 {total_wait} 秒）...", flush=True)
    start_time = time.time()
    seen_video_duration = 0

    while time.time() - start_time < total_wait:
        v_info = driver.execute_script("""
            var vids = Array.from(document.querySelectorAll('video'));
            for (var v of vids) {
                if (v.duration > 0) {
                    return {
                        current: Math.floor(v.currentTime),
                        duration: Math.floor(v.duration),
                        ended: v.ended,
                        paused: v.paused
                    };
                }
            }
            return null;
        """)

        if v_info:
            cur = v_info.get("current", 0)
            dur = v_info.get("duration", 0)
            ended = v_info.get("ended", False)
            seen_video_duration = max(seen_video_duration, dur)

            print(f"  📺 视频广告播放中: [{cur}s / {dur}s]，正在监控...", flush=True)

            # 播满或进入最后两秒时判定为完成
            if ended or (dur > 0 and cur >= dur - 2):
                print("  🎉 视频广告已完整走完规定时长！保存现场快照...", flush=True)
                capture_screenshot_smart(driver, "g4f_ad_completed.png")
                time.sleep(2)
                break
            
            time.sleep(3)
            continue

        if seen_video_duration > 0:
            if time.time() - start_time < (seen_video_duration + 5):
                time.sleep(2)
                continue
            else:
                print("  🎉 视频广告总计时已满！保存现场快照...", flush=True)
                capture_screenshot_smart(driver, "g4f_ad_completed.png")
                time.sleep(2)
                break

        if time.time() - start_time >= 25:
            print("  ⏱️ 广告展示时间已达标，保存现场快照...", flush=True)
            capture_screenshot_smart(driver, "g4f_ad_completed.png")
            time.sleep(2)
            break

        time.sleep(2)

    # 针对右上角圆圈叉号进行点击
    print("  🎯 正在精准点击视频窗口右上角的关闭按钮...", flush=True)
    for _ in range(3):
        if kill_video_close_button_precision(driver):
            print("  👉 已成功命中并点击视频/广告右上角关闭按钮！", flush=True)
            break
        time.sleep(1)

    time.sleep(3)
    return True


def robust_click_free_button(driver):
    print("🔍 正在确保侧边栏滚动并锁定 [+ 90 min] 续期按钮...", flush=True)
    force_scroll_and_reveal_session_card(driver)

    target = None
    selectors = [
        "//button[contains(., '90 min') or contains(., '+ 90')]",
        "//div[contains(text(), 'Active session')]/ancestor::div[contains(@class, 'card') or contains(@class, 'session') or contains(@class, 'rounded')]//button[1]",
        "//*[contains(text(), 'Active session')]/following::button[contains(., '90')]"
    ]
    for sel in selectors:
        elements = driver.find_elements(By.XPATH, sel)
        for el in elements:
            try:
                txt = el.text.strip().lower()
                if any(bad in txt for bad in ["$", "0.15", "24h"]):
                    continue
                driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", el)
                time.sleep(0.3)
                if el.is_displayed():
                    target = el
                    break
            except Exception:
                continue
        if target:
            break

    if not target:
        print("❌ 未能在页面中捕获到有效的续期按钮！", flush=True)
        return False

    print(f"🎯 成功锁定目标按钮: [{target.text.strip()}]，派发点击...", flush=True)

    try:
        driver.execute_script("""
            var el = arguments[0];
            el.scrollIntoView({behavior: 'instant', block: 'center'});
        """, target)
        time.sleep(0.3)
    except Exception:
        pass

    try:
        ActionChains(driver).move_to_element(target).pause(0.2).click().perform()
    except Exception:
        pass

    try:
        driver.execute_script("""
            function fireAll(elem) {
                if (!elem) return;
                ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(function(eventType) {
                    var ev = new MouseEvent(eventType, {
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: elem.getBoundingClientRect().left + 10,
                        clientY: elem.getBoundingClientRect().top + 10
                    });
                    elem.dispatchEvent(ev);
                });
                if (elem.click) elem.click();
            }
            var btn = arguments[0];
            fireAll(btn);
            var children = btn.querySelectorAll('*');
            children.forEach(function(c) { fireAll(c); });
        """, target)
    except Exception:
        try:
            target.click()
        except Exception:
            pass

    return True


def ensure_sidebar_expanded(driver):
    try:
        if "Active session" in driver.page_source:
            return
        menu_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(@class, 'menu') or contains(@aria-label, 'menu')] | //header//button | //nav//button"
        )
        for mb in menu_btns:
            try:
                if mb.is_displayed():
                    ActionChains(driver).move_to_element(mb).click().perform()
                    time.sleep(1.5)
                    break
            except Exception:
                continue
    except Exception:
        pass


def inject_cookies_and_navigate(driver, raw_cookie_str: str) -> bool:
    print("🌐 正在初始化会话并注入 Cookie...", flush=True)
    non_blocking_navigate(driver, BASE_URL, wait_seconds=3)

    for item in raw_cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, val = item.split("=", 1)
        for dom in ["control.gaming4free.net", ".gaming4free.net"]:
            cookie_dict = {
                "name": name.strip(),
                "value": val.strip(),
                "domain": dom,
                "path": "/",
            }
            try:
                driver.add_cookie(cookie_dict)
            except Exception:
                pass

    target_url = CONSOLE_URL if CONSOLE_URL else f"{BASE_URL}/server/c2d0a619/console"
    print(f"🚀 直达目标控制台: {target_url} ...", flush=True)
    non_blocking_navigate(driver, target_url, wait_seconds=5)

    if "login" in driver.current_url.lower():
        print("❌ Cookie 凭据失效，当前停留在登录页！", flush=True)
        return False

    body_text = driver.get_text("body")
    if "ADD SERVER SLOT" in body_text or "/servers" in driver.current_url:
        open_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'OPEN')] | //a[contains(., 'OPEN')] | //div[contains(@class, 'button') and contains(., 'OPEN')]"
        )
        for btn in open_btns:
            try:
                if btn.is_displayed():
                    ActionChains(driver).move_to_element(btn).click().perform()
                    break
            except Exception:
                continue
        time.sleep(4)

    ensure_sidebar_expanded(driver)
    force_scroll_and_reveal_session_card(driver)
    return True


def get_console_info(driver):
    force_scroll_and_reveal_session_card(driver)
    body = driver.get_text("body")
    remaining_text = "未知"

    m = re.search(r"(\d{1,2}:\d{2}:\d{2})\s*remaining", body, re.IGNORECASE)
    if m:
        remaining_text = m.group(1).strip()

    server_status = "ONLINE"
    if "OFFLINE" in body.upper():
        server_status = "OFFLINE"
    elif "STARTING" in body.upper():
        server_status = "STARTING"
    elif "STOPPING" in body.upper():
        server_status = "STOPPING"

    return server_status, remaining_text


def time_to_seconds(t_str: str) -> int:
    if not t_str or ":" not in t_str:
        return 0
    parts = [int(p) for p in t_str.split(":") if p.isdigit()]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return 0


def do_renew_and_start(driver):
    ensure_sidebar_expanded(driver)
    force_scroll_and_reveal_session_card(driver)
    server_status, remaining_before = get_console_info(driver)
    start_action = "正常运行"

    if "OFFLINE" in server_status:
        start_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'START') or contains(., 'Start')] | //*[contains(@class, 'green') and contains(., 'START')]"
        )
        for sb in start_btns:
            try:
                if sb.is_displayed() and "RESTART" not in sb.text.upper():
                    ActionChains(driver).move_to_element(sb).click().perform()
                    start_action = "⚡ 已执行开机"
                    time.sleep(3)
                    break
            except Exception:
                continue

    # 检查冷却状态
    body = driver.get_text("body")
    cd_match = re.search(r"(\d{1,2}:\d{2})\s*cd", body, re.IGNORECASE)
    if cd_match:
        cd_str = cd_match.group(0).strip()
        print(f"⏳ 检测到续期处于官方 5 分钟冷却中 [{cd_str}]，安全跳过。", flush=True)
        return server_status, remaining_before, remaining_before, False, start_action, f"⏳ 处于官方冷却中 ({cd_str})"

    renew_executed = robust_click_free_button(driver)
    action_desc = "ℹ️ 未能触发按钮"

    if renew_executed:
        time.sleep(2)
        if is_real_turnstile_modal(driver):
            solve_turnstile_if_present(driver, max_wait=20)
        handle_video_and_ad_lifecycle(driver, total_wait=50)
        action_desc = "已完成验证与广告交互，等待时长入账"

    print("🔄 刷新控制台页面以准确同步剩余倒计时...", flush=True)
    driver.refresh()
    time.sleep(5)
    ensure_sidebar_expanded(driver)
    force_scroll_and_reveal_session_card(driver)

    server_status_after, remaining_after = get_console_info(driver)

    sec_before = time_to_seconds(remaining_before)
    sec_after = time_to_seconds(remaining_after)

    if sec_after - sec_before >= 3000:
        added_min = (sec_after - sec_before) // 60
        action_desc = f"✅ 成功续期（时长增加约 {added_min} 分钟）"
        renew_executed = True
    elif sec_before > 0 and sec_after > 0 and sec_after <= sec_before:
        action_desc = "⚠️ 倒计时未增加（可能处于冷却或频控）"

    return server_status_after, remaining_before, remaining_after, renew_executed, start_action, action_desc


def main():
    print("=== Gaming4Free 自动续期巡检启动 (现场诊断+视频右上角关闭版) ===", flush=True)

    if not G4F_COOKIE:
        print("❌ 未配置 G4F_COOKIE 环境变量，请在 Secrets 中添加！", flush=True)
        return

    setup_network_proxy()

    current_ip = get_current_ip()
    print(f"🎯 当前出口 IP: {current_ip}", flush=True)

    chromium_args = [
        "--start-maximized",
        "--window-size=1920,1080",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--page-load-strategy=none",
    ]
    if IS_PROXY and PROXY_SERVER:
        chromium_args.append(f"--proxy-server={PROXY_SERVER}")
        print(f"⚙️ 浏览器已挂载代理: {PROXY_SERVER}", flush=True)

    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))
    try:
        driver.maximize_window()
        driver.set_page_load_timeout(15)
        driver.set_script_timeout(15)
    except Exception:
        pass

    try:
        if not inject_cookies_and_navigate(driver, G4F_COOKIE):
            capture_screenshot_smart(driver, "g4f_cookie_failed.png")
            tg_send(f"🔴 <b>Gaming4Free Cookie 登录失效</b>\nIP: {current_ip}", photo_path="g4f_cookie_failed.png")
            return

        status, rem_before, rem_after, renewed, start_action, action_desc = do_renew_and_start(driver)
        print(f"📊 状态: {status} | 续期前: {rem_before} | 续期后: {rem_after} | 动作: {action_desc}", flush=True)

        time.sleep(2)
        capture_screenshot_smart(driver, "g4f_result.png")

        # 若倒计时未增加，优先将广告播完瞬间的现场截图推送到 Telegram
        send_pic = "g4f_result.png"
        if not renewed and os.path.exists("g4f_ad_completed.png"):
            send_pic = "g4f_ad_completed.png"

        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

        tg_send(
            f"📋 <b>Gaming4Free 续期巡检报告</b>\n\n"
            f"🔑 <b>认证方式：</b><code>Cookie 免登</code>\n"
            f"🖥️ <b>实例电源：</b><code>{status}</code>\n"
            f"⚡ <b>开机操作：</b><code>{start_action}</code>\n"
            f"⏳ <b>续期前时间：</b><code>{rem_before}</code>\n"
            f"⌛ <b>续期后时间：</b><code>{rem_after}</code>\n"
            f"📊 <b>执行动作：</b><code>{action_desc}</code>\n"
            f"🌐 <b>出口 IP：</b><code>{current_ip}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>",
            photo_path=send_pic
        )
        print("✅ Gaming4Free 任务执行完毕！", flush=True)

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}", flush=True)
        capture_screenshot_smart(driver, "g4f_error.png")
        tg_send(f"🔴 <b>Gaming4Free 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="g4f_error.png")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
