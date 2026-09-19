#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Gaming4Free 自动续期与开关机巡检 (移除阻塞式拦截，加入超时熔断版)
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

    if raw.startswith(("vless://", "vmess://")):
        print("⚙️ 检测到节点链接，准备启动 sing-box 本地代理...", flush=True)
        bin_path = shutil.which("sing-box")
        if not bin_path:
            print("❌ 系统中找不到 sing-box 可执行程序", flush=True)
            sys.exit(1)

        try:
            if raw.startswith("vmess://"):
                outbound = _parse_vmess(raw)
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


def physical_click(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        time.sleep(0.3)
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(element).pause(0.2).click().perform()
        return
    except Exception:
        pass
    try:
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def fast_navigate(driver, url, wait_seconds=5):
    """通过 location.href 实现非阻塞式快速跳转"""
    try:
        driver.execute_script(f"window.location.href = '{url}';")
    except Exception:
        try:
            driver.get(url)
        except Exception:
            pass
    time.sleep(wait_seconds)


def is_cf_challenge_present(driver):
    try:
        src = driver.page_source
        if "challenges.cloudflare.com" in src or "cf-turnstile" in src or "Verify you" in src:
            return True
        iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")
        for f in iframes:
            try:
                if f.is_displayed():
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def solve_turnstile_captcha(driver, max_wait=16):
    """快速穿透 Turnstile，非阻塞并具有超时保护"""
    print("🛡️ 正在尝试突破 Cloudflare Turnstile 人机验证...", flush=True)
    end_time = time.time() + max_wait

    while time.time() < end_time:
        # 1. 检查 Token
        try:
            token = driver.execute_script(
                "var el = document.querySelector('[name=\"cf-turnstile-response\"]'); return el ? el.value : '';"
            )
            if token and len(token) > 20:
                print("  ✅ Turnstile Token 已就绪，验证通过！", flush=True)
                return True
        except Exception:
            pass

        # 2. 内置 GUI 点击
        try:
            driver.uc_gui_click_captcha()
            time.sleep(2)
        except Exception:
            pass

        # 3. 物理 CDP 坐标点击
        try:
            iframes = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")
            for frame in iframes:
                try:
                    if not frame.is_displayed():
                        continue
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", frame)
                    time.sleep(0.2)
                    rect = driver.execute_script(
                        "var r = arguments[0].getBoundingClientRect(); return {x: r.left + 35, y: r.top + r.height / 2};",
                        frame
                    )
                    if rect and rect.get("x", 0) > 0:
                        driver.execute_cdp_cmd(
                            "Input.dispatchMouseEvent",
                            {"type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1}
                        )
                        time.sleep(0.08)
                        driver.execute_cdp_cmd(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left"}
                        )
                        print(f"  👉 已对准 Turnstile 坐标 ({int(rect['x'])}, {int(rect['y'])}) 发送物理点击", flush=True)
                        time.sleep(2.5)
                        break
                except Exception:
                    continue
        except Exception:
            pass

        time.sleep(1)

    token_final = driver.execute_script(
        "var el = document.querySelector('[name=\"cf-turnstile-response\"]'); return el ? el.value : '';"
    )
    passed = bool(token_final and len(token_final) > 20) or not is_cf_challenge_present(driver)
    if passed:
        print("  ✅ Cloudflare 验证已突破！", flush=True)
    else:
        print("  ❌ Turnstile 验证未能完成突破！", flush=True)
    return passed


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
                    physical_click(driver, mb)
                    print("  👉 检测到侧边栏折叠，已点击展开！", flush=True)
                    time.sleep(1.5)
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"⚠️ 展开侧边栏提示: {e}", flush=True)


def inject_cookies_and_navigate(driver, raw_cookie_str: str) -> bool:
    print("🌐 正在初始化域名会话并注入 Cookie...", flush=True)
    fast_navigate(driver, BASE_URL, wait_seconds=4)
    solve_turnstile_captcha(driver, max_wait=6)

    for item in raw_cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, val = item.split("=", 1)
        cookie_dict = {
            "name": name.strip(),
            "value": val.strip(),
            "domain": "control.gaming4free.net",
            "path": "/",
        }
        try:
            driver.add_cookie(cookie_dict)
        except Exception as e:
            print(f"  ⚠️ Cookie 注入提示 ({name}): {e}", flush=True)

    target_url = CONSOLE_URL if CONSOLE_URL else f"{BASE_URL}/server/c2d0a619/console"
    print(f"🚀 直达控制台页面: {target_url} ...", flush=True)
    fast_navigate(driver, target_url, wait_seconds=5)
    solve_turnstile_captcha(driver, max_wait=6)

    if "login" in driver.current_url.lower():
        print("❌ Cookie 已失效或无效，页面仍停留在登录页！", flush=True)
        return False

    body_text = driver.get_text("body")
    if "ADD SERVER SLOT" in body_text or "/servers" in driver.current_url:
        print("📌 停留在列表页，点击 [OPEN] 按钮进入控制台...", flush=True)
        open_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'OPEN')] | //a[contains(., 'OPEN')] | //div[contains(@class, 'button') and contains(., 'OPEN')]"
        )
        clicked = False
        for btn in open_btns:
            try:
                if btn.is_displayed():
                    physical_click(driver, btn)
                    clicked = True
                    print("  👉 成功点击 [OPEN] 按钮！", flush=True)
                    break
            except Exception:
                continue
        if not clicked:
            cards = driver.find_elements(By.XPATH, "//*[contains(@class, 'server') or contains(., 'myeubopu')]")
            for c in cards:
                try:
                    if c.is_displayed():
                        physical_click(driver, c)
                        print("  👉 点击服务器卡片进入！", flush=True)
                        break
                except Exception:
                    continue
        time.sleep(5)
        solve_turnstile_captcha(driver, max_wait=6)

    ensure_sidebar_expanded(driver)
    print(f"🎉 当前已在控制台页面: {driver.current_url}", flush=True)
    return True


def get_console_info(driver):
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
    server_status, remaining_before = get_console_info(driver)
    start_action = "正常运行"

    # 1. 开关机巡检
    if "OFFLINE" in server_status:
        print("⚡ 服务器处于 OFFLINE 状态，尝试点击 START 开机...", flush=True)
        start_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'START') or contains(., 'Start')] | //*[contains(@class, 'green') and contains(., 'START')]"
        )
        for sb in start_btns:
            try:
                if sb.is_displayed() and "RESTART" not in sb.text.upper():
                    physical_click(driver, sb)
                    print("  👉 已点击 START 开机按钮！", flush=True)
                    start_action = "⚡ 已执行开机"
                    time.sleep(4)
                    break
            except Exception:
                continue

    # 2. 检查冷却倒计时
    body = driver.get_text("body")
    cd_match = re.search(r"(\d{1,2}:\d{2})\s*cd", body, re.IGNORECASE)
    if cd_match:
        cd_str = cd_match.group(0).strip()
        print(f"⏳ 检测到续期处于冷却中 [{cd_str}]，安全跳过本次点击。", flush=True)
        return server_status, remaining_before, remaining_before, False, start_action, f"⏳ 处于冷却中 ({cd_str})"

    # 3. 定位「+ 90 min」
    print("🔍 正在定位左侧栏底部的免费续期按钮 [+ 90 min] ...", flush=True)
    renew_executed = False

    free_candidates = driver.find_elements(
        By.XPATH,
        "//button[contains(., '90 min') or contains(., '90min') or contains(., '+ 90')] | "
        "//*[contains(text(), '90 min') or contains(text(), '+ 90')]/ancestor-or-self::button | "
        "//*[contains(@class, 'button') and contains(., '90 min')] | "
        "//*[contains(text(), '90 min') or contains(text(), '+ 90')]"
    )

    valid_free_btn = None
    for btn in free_candidates:
        try:
            btn_text = btn.text.strip().lower()
            if any(bad in btn_text for bad in ["$", "0.15", "24h", "pro", "pay", "always"]):
                continue
            if "90 min" in btn_text or "+ 90" in btn_text:
                valid_free_btn = btn
                break
        except Exception:
            continue

    if valid_free_btn:
        try:
            btn_disabled = valid_free_btn.get_attribute("disabled") or "disabled" in (valid_free_btn.get_attribute("class") or "").lower()
        except Exception:
            btn_disabled = False

        if btn_disabled:
            print("ℹ️ [+ 90 min] 按钮处于禁用状态，跳过本次续期。", flush=True)
            action_desc = "ℹ️ 按钮处于禁用状态"
        else:
            print(f"🎯 成功锁定免费续期按钮，执行点击...", flush=True)
            physical_click(driver, valid_free_btn)
            time.sleep(2)

            cf_passed = True
            if is_cf_challenge_present(driver):
                cf_passed = solve_turnstile_captcha(driver, max_wait=16)
                time.sleep(3)

            if not cf_passed:
                print("❌ Cloudflare 人机验证未通过，未能提交续期！", flush=True)
                action_desc = "❌ Cloudflare 验证未通过"
            else:
                renew_executed = True
                action_desc = "已点击 +90 min 按钮（等待判定）"
    else:
        print("ℹ️ 未发现可用的 [+ 90 min] 免费按钮（可能冷却中或已被顶满）", flush=True)
        action_desc = "ℹ️ 未发现可用免费按钮"

    # 等待页面更新倒计时
    print("⏳ 等待控制台状态与倒计时刷新...", flush=True)
    time.sleep(6)
    server_status_after, remaining_after = get_console_info(driver)

    # 4. 严密对比时间增量
    sec_before = time_to_seconds(remaining_before)
    sec_after = time_to_seconds(remaining_after)

    if renew_executed:
        if sec_after - sec_before >= 3000:
            added_min = (sec_after - sec_before) // 60
            action_desc = f"✅ 续期生效（时长增加约 {added_min} 分钟）"
        elif sec_before > 0 and sec_after > 0 and sec_after <= sec_before:
            action_desc = "⚠️ 倒计时未增加（可能已达上限或验证码提交被拦截）"
        else:
            action_desc = "ℹ️ 续期已提交，已刷新面板状态"

    return server_status_after, remaining_before, remaining_after, renew_executed, start_action, action_desc


def main():
    print("=== Gaming4Free 自动续期巡检启动 ===", flush=True)

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
        "--page-load-strategy=eager",
    ]
    if IS_PROXY and PROXY_SERVER:
        chromium_args.append(f"--proxy-server={PROXY_SERVER}")
        print(f"⚙️ 浏览器已挂载代理: {PROXY_SERVER}", flush=True)

    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))
    try:
        driver.maximize_window()
        driver.set_page_load_timeout(25)
        driver.set_script_timeout(25)
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
            photo_path="g4f_result.png"
        )
        print("✅ Gaming4Free 任务执行完毕！", flush=True)

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}", flush=True)
        capture_screenshot_smart(driver, "g4f_error.png")
        tg_send(f"🔴 <b>Gaming4Free 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="g4f_error.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
