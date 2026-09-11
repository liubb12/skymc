#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SkyMC 自动续期脚本 v14 (假人在线模式 + 精准点击 Renew 续杯)
"""

import os
import sys
import time
import json
import atexit
import base64
import shutil
import socket
import subprocess
from urllib.parse import urlparse, parse_qs, unquote
import requests
from seleniumbase import SB

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


def _parse_socks(link: str) -> dict:
    clean = link
    if clean.startswith("socks5://"):
        clean = clean[len("socks5://"):]
    elif clean.startswith("socks://"):
        clean = clean[len("socks://"):]

    clean = clean.split("#")[0].strip()
    auth_part = ""
    server_part = ""
    if "@" in clean:
        auth_part, server_part = clean.split("@", 1)
    else:
        server_part = clean

    username = ""
    password = ""
    if auth_part:
        try:
            decoded_auth = _b64decode(auth_part).decode("utf-8")
            if ":" in decoded_auth:
                username, password = decoded_auth.split(":", 1)
        except Exception:
            if ":" in auth_part:
                username, password = auth_part.split(":", 1)

    if ":" in server_part:
        host, port_str = server_part.split(":", 1)
        port = int(port_str)
    else:
        host = server_part
        port = 1080

    print(f"   [Socks5 解析] 连接目标: {host}:{port}")
    outbound = {
        "type": "socks",
        "tag": "proxy",
        "server": host,
        "server_port": port,
        "version": "5",
    }
    if username:
        outbound["username"] = username
        outbound["password"] = password
    return outbound


def _parse_vmess(link: str) -> dict:
    raw = link[len("vmess://"):]
    obj = json.loads(_b64decode(raw).decode("utf-8"))

    raw_server = obj.get("add") or ""
    port = int(obj.get("port") or 443)
    uuid = obj.get("id") or ""
    net = (obj.get("net") or "tcp").lower()
    tls_on = str(obj.get("tls") or "").lower() in ("tls", "reality", "1", "true")

    actual_domain = obj.get("host") or obj.get("sni")
    server = actual_domain if (actual_domain and ".xyz" not in actual_domain) else raw_server
    sni = actual_domain or raw_server
    ws_host = actual_domain or raw_server
    
    raw_path = obj.get("path") or "/"
    ws_path = raw_path.split("?")[0] if "?" in raw_path else raw_path

    outbound = {
        "type": "vmess",
        "tag": "proxy",
        "server": server,
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
            "path": ws_path,
            "headers": {"Host": ws_host},
        }
    return outbound


def _parse_vless(link: str) -> dict:
    parsed = urlparse(link)
    uuid = unquote(parsed.username or "")
    raw_server = parsed.hostname or ""
    port = parsed.port or 443
    q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    security = (q.get("security") or "none").lower()
    net = (q.get("type") or "tcp").lower()

    actual_domain = q.get("sni") or q.get("host")
    server = actual_domain if (actual_domain and ".xyz" not in actual_domain) else raw_server
    sni = actual_domain or raw_server
    ws_host = actual_domain or raw_server
    
    raw_path = q.get("path") or "/"
    ws_path = raw_path.split("?")[0] if "?" in raw_path else raw_path

    outbound = {
        "type": "vless",
        "tag": "proxy",
        "server": server,
        "server_port": int(port),
        "uuid": uuid,
        "flow": q.get("flow") or "",
        "packet_encoding": "xudp",
    }
    if security in ("tls", "reality"):
        outbound["tls"] = {
            "enabled": True,
            "server_name": sni,
            "utls": {"enabled": True, "fingerprint": q.get("fp") or "chrome"},
        }
    if net == "ws":
        outbound["transport"] = {
            "type": "ws",
            "path": ws_path,
            "headers": {"Host": ws_host},
        }
    return outbound


def build_singbox_config(node_link: str, listen_port: int) -> dict:
    link = node_link.strip()
    if link.startswith("socks://") or link.startswith("socks5://"):
        outbound = _parse_socks(link)
    elif link.startswith("vmess://"):
        outbound = _parse_vmess(link)
    elif link.startswith("vless://"):
        outbound = _parse_vless(link)
    else:
        raise ValueError("NODE_LINK 仅支持 socks5://、vless:// 或 vmess://")

    return {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [
                {"tag": "dns-remote", "address": "https://1.1.1.1/dns-query", "detour": "direct"}
            ]
        },
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
    print("⚙️ 检测到 NODE_LINK，准备启动 sing-box 代理...")
    scheme = NODE_LINK.split("://", 1)[0].lower() if "://" in NODE_LINK else "?"
    print(f"   协议: {scheme}://  本地端口: {SINGBOX_PORT}")

    bin_path = shutil.which("sing-box")
    if not bin_path:
        print("❌ 未找到 sing-box 二进制")
        sys.exit(1)

    try:
        cfg = build_singbox_config(NODE_LINK, SINGBOX_PORT)
    except Exception as e:
        print(f"❌ NODE_LINK 解析失败: {e}")
        sys.exit(1)

    cfg_path = "/tmp/sing-box-skymc.json"
    log_path = "/tmp/sing-box-skymc.log"
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    logf = open(log_path, "wb")
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
            print(f"✅ sing-box 本地端口已监听: {PROXY_SERVER}")

            time.sleep(1)
            try:
                test = requests.get("https://api.ip.sb/ip", proxies=REQUESTS_PROXIES, timeout=8)
                print(f"🎯 节点握手成功！出口 IP: {test.text.strip()}")
            except Exception as err:
                print(f"❌ 节点握手失败: {err}")
            return
        time.sleep(0.4)

    print("❌ sing-box 启动异常")
    sys.exit(1)


def send_tg(token, chat_id, message, image_path=None):
    if not token or not chat_id:
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
                print("📨 Telegram 通知已发送（附带图片）")
                return
        except Exception:
            pass
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
            proxies=REQUESTS_PROXIES,
        )
        print("📨 Telegram 通知已发送（纯文字）")
    except Exception as e:
        print(f"❌ Telegram 发送异常: {e}")


def get_current_ip():
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=REQUESTS_PROXIES, timeout=8)
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
    print("🛡 检测到 Cloudflare 人机验证弹窗，开始处理...")
    for i in range(max_retry):
        try:
            sb.uc_gui_click_captcha()
            time.sleep(5)
            if not challenge_visible(sb):
                print("   ✅ 验证已通过")
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
    wait_challenge_gone(sb, timeout=12)
    time.sleep(1)
    sb.save_screenshot(path)
    print(f"📸 已保存截图: {path}")
    return path


def fill_field(sb, selectors, value, field_name, timeout=20):
    print(f"   填写 {field_name} ...")
    end = time.time() + timeout
    last_err = None
    while time.time() < end:
        for sel in selectors:
            try:
                sb.wait_for_element_visible(sel, timeout=2)
                sb.clear(sel)
                sb.type(sel, value)
                print(f"   ✅ {field_name} 已填写（{sel}）")
                return True
            except Exception as e:
                last_err = e
                continue
        time.sleep(1)
    print(f"   ❌ 无法填写 {field_name}，最后错误: {last_err}")
    return False


def click_login(sb):
    selectors = [
        'button:contains("Login")',
        'button[type="submit"]',
        'button:contains("Sign in")',
    ]
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                print(f"   已点击登录（{sel}）")
                return True
        except Exception:
            continue
    return False


def login(sb, email, password):
    print("🌐 打开登录页面...")
    try:
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=6)
    except Exception:
        sb.open(LOGIN_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(3)

    handle_cloudflare(sb)
    time.sleep(1)

    print("📧 填写邮箱/用户名...")
    if not fill_field(sb, EMAIL_SELECTORS, email, "邮箱", timeout=25):
        safe_screenshot(sb, "login_failed.png")
        return False

    time.sleep(0.5)

    print("🔑 填写密码...")
    if not fill_field(sb, PASSWORD_SELECTORS, password, "密码", timeout=15):
        safe_screenshot(sb, "login_failed.png")
        return False

    time.sleep(1)
    handle_cloudflare(sb)
    time.sleep(2)

    for attempt in range(5):
        print(f"🔑 点击登录按钮...(第 {attempt + 1} 次)")
        click_login(sb)
        time.sleep(3)
        if challenge_visible(sb):
            handle_cloudflare(sb, max_retry=4)
            time.sleep(3)
        for _ in range(10):
            url = (sb.get_current_url() or "").lower()
            if "login" not in url:
                print(f"✅ 登录成功！当前页面: {sb.get_current_url()}")
                return True
            if challenge_visible(sb):
                handle_cloudflare(sb, max_retry=2)
            time.sleep(1)
        time.sleep(2)

    print(f"❌ 登录失败，当前 URL: {sb.get_current_url()}")
    safe_screenshot(sb, "login_failed.png")
    return False


def open_server_panel(sb):
    print("📄 进入服务器面板...")
    try:
        sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=5)
    except Exception:
        sb.open(SERVER_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(4)
    handle_cloudflare(sb)
    wait_challenge_gone(sb, timeout=15)
    time.sleep(2)


def read_panel_info(sb):
    """精准读取页面状态、运行倒计时、归档时间、Renew 按钮存在性"""
    script = r"""
        var body = (document.body && document.body.innerText) ? document.body.innerText : '';
        var status = 'unknown';
        if (/\bOnline\b/i.test(body)) status = 'Online';
        else if (/\bStarting\b/i.test(body)) status = 'Starting';
        else if (/\bStopping\b/i.test(body)) status = 'Stopping';
        else if (/\bOffline\b/i.test(body) || /\bStopped\b/i.test(body)) status = 'Offline';

        // 提取倒计时（如 59:49）
        var countdown = null;
        var m = body.match(/\b([0-5]?\d:[0-5]\d)\b/);
        if (m) countdown = m[1];

        // 提取归档时间
        var archiveDate = null;
        var arch = body.match(/ARCHIVE DATE\s*[:\n\r]*([^\n\r]+)/i);
        if (arch) archiveDate = arch[1].trim();

        // 检查是否有 Stop 和 Renew 按钮
        var hasStop = false;
        var hasRenew = false;
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            var b = btns[i];
            var txt = (b.innerText || '').toLowerCase();
            if (b.offsetParent === null) continue;
            if (txt.indexOf('stop') >= 0) hasStop = true;
            if (txt.indexOf('renew') >= 0) hasRenew = true;
        }
        return JSON.stringify({
            status: status,
            countdown: countdown,
            archiveDate: archiveDate,
            hasStop: hasStop,
            hasRenew: hasRenew
        });
    """
    try:
        raw = sb.execute_script(script)
        info = json.loads(raw) if raw else {}
    except Exception:
        info = {}
    print(f"   [面板状态] 状态: {info.get('status')} | 倒计时: {info.get('countdown')} | Renew按钮: {info.get('hasRenew')}")
    return info


def click_start_if_needed(sb, info):
    status = (info.get("status") or "").lower()
    if status == "online" or info.get("hasStop"):
        return True, "已在运行"

    print("🔌 服务器离线，点击 Start 启动...")
    for sel in ['button:contains("Start")', 'button:contains("启动")']:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                break
        except Exception:
            continue
    time.sleep(5)
    handle_cloudflare(sb)
    for _ in range(12):
        cur = read_panel_info(sb)
        if (cur.get("status") or "").lower() == "online":
            return True, "已启动进入在线状态"
        time.sleep(3)
    return False, "尝试启动超时"


def click_renew(sb):
    """精准定位并点击截图里的 Renew 按钮"""
    print("🔍 查找 Renew 按钮...")
    selectors = [
        'button:contains("Renew")',
        'button:has(svg.fa-clock)',
        'button[aria-label*="renew" i]'
    ]
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                print(f"✅ 已点击 Renew 按钮（{sel}）")
                return True
        except Exception:
            continue

    # JS 兜底查找
    try:
        res = sb.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var b = btns[i];
                if ((b.innerText || '').indexOf('Renew') !== -1) {
                    b.click();
                    return true;
                }
            }
            return false;
        """)
        if res:
            print("✅ 已通过 JS 点击 Renew 按钮")
            return True
    except Exception as e:
        print(f"❌ 点击 Renew 异常: {e}")

    print("⚠️ 未找到 Renew 按钮（若假人未在服中，面板可能不显示此按钮）")
    return False


def main():
    if not EMAIL or not PASSWORD:
        print("❌ 请设置环境变量 SKYMC_EMAIL 和 SKYMC_PASSWORD")
        sys.exit(1)

    print("🚀 启动 SkyMC 自动续期脚本 v14")
    start_singbox_from_node_link()

    current_ip = get_current_ip()
    print(f"🎯 当前出口 IP: {current_ip}")

    sb_kwargs = {"uc": True, "headless": False, "locale_code": "en"}
    if IS_PROXY:
        sb_kwargs["proxy"] = PROXY_SERVER
        print(f"⚙️ 已启用代理: {PROXY_SERVER}")

    with SB(**sb_kwargs) as sb:
        if not login(sb, EMAIL, PASSWORD):
            msg = f"❌ 登录失败\nIP: {current_ip}"
            print(msg)
            send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg, image_path="login_failed.png")
            return

        open_server_panel(sb)
        before = read_panel_info(sb)
        before_time = before.get("countdown") or "未知"

        # 1. 确保开机
        click_start_if_needed(sb, before)

        # 2. 点击 Renew 按钮（假人在服时直接续杯）
        renew_clicked = click_renew(sb)

        # 3. 等待刷新并抓取最终状态
        time.sleep(4)
        handle_cloudflare(sb)
        after = read_panel_info(sb)
        after_time = after.get("countdown") or "未知"
        archive_date = after.get("archiveDate") or before.get("archiveDate") or "未知"

        safe_screenshot(sb, "final_result.png")

        lines = [
            f"{'✅' if renew_clicked else 'ℹ️'} SkyMC 续期执行完毕",
            f"服务器: {SERVER_ID}",
            f"运行状态: {after.get('status')}",
            f"Renew 按钮: {'已点击续杯' if renew_clicked else '未出现(假人未在服或已满)'}",
            f"倒计时变化: {before_time} -> {after_time}",
            f"归档截止日: {archive_date}",
            f"出口 IP: {current_ip}",
        ]
        msg = "\n".join(lines)
        print("\n" + msg)

        img = "final_result.png" if os.path.exists("final_result.png") else None
        send_tg(TG_BOT_TOKEN, TG_CHAT_ID, msg, image_path=img)

    print("🏁 脚本执行完毕")


if __name__ == "__main__":
    main()
