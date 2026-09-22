#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCServerHost 自动监控脚本（GitHub Actions + Xvfb 适配版）
- 在线 / 离线 / 未知 一律发 TG 通知
- 配合 GabrielBB/xvfb-action 提供虚拟显示
- UC 模式自动同步真实 Chrome 指纹，不写死 UA
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
    """检测 Cloudflare 盾"""
    try:
        src = sb.get_page_source()[:8000].lower()
        markers = [
            "verify you are human",
            "security verification",
            "please complete the captcha",
            "challenges.cloudflare.com",
            "cf-challenge",
            "cf_chl_opt",
            "cf-turnstile",
        ]
        return any(m in src for m in markers)
    except Exception:
        return False

def bypass_cf(sb, max_retry=4):
    """过 CF 盾"""
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

def control_server(sb):
    """检查状态并尝试开机"""
    log("📡 打开服务器面板...")
    try:
        sb.uc_open_with_reconnect(SERVERS_URL, reconnect_time=4)
    except Exception:
        sb.open(SERVERS_URL)
    time.sleep(4)
    bypass_cf(sb, max_retry=2)

    screenshot(sb, "mc_dashboard.png")
    body = sb.get_text("body").lower()

    if "running" in body:
        log("🟢 服务器状态：运行中，无需操作")
        return "运行中", "无需操作"

    if "starting" in body:
        log("🟡 服务器正在启动中")
        return "启动中", "无需操作"

    if "offline" not in body and "stopped" not in body:
        log("⚠️ 无法识别状态，请查看截图")
        return "未知状态", "未操作"

    log("🔴 检测到服务器离线，尝试唤醒...")
    clicked = False
    try:
        buttons = sb.driver.find_elements(By.CSS_SELECTOR, "button")
        for btn in buttons:
            try:
                if not btn.is_displayed():
                    continue
                inner = (btn.get_attribute("innerHTML") or "").lower()
                keywords = [
                    "data-lucide=\"play\"",
                    "lucide-play",
                    ">play<",
                    ">start<",
                    "开机",
                    "启动",
                    "power on",
                    "boot",
                ]
                if any(kw in inner for kw in keywords):
                    sb.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", btn
                    )
                    time.sleep(0.5)
                    ActionChains(sb.driver).move_to_element(btn).click().perform()
                    log("⚡ 已点击开机按钮")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            # JS 兜底：按文案点击
            sb.driver.execute_script("""
                document.querySelectorAll('button').forEach(b => {
                    const t = (b.textContent || '').toLowerCase();
                    if (t.includes('start') || t.includes('power') || t.includes('boot')) b.click();
                });
            """)
            log("⚡ JS 兜底点击已执行")
            clicked = True
    except Exception as e:
        log(f"⚠️ 点击开机按钮异常: {e}")
        return "离线", "开机失败"

    if clicked:
        time.sleep(6)
        sb.driver.refresh()
        time.sleep(4)
        final = sb.get_text("body").lower()
        if "running" in final or "starting" in final:
            screenshot(sb, "mc_final.png")
            return "启动中/运行中", "已唤醒"
        screenshot(sb, "mc_final.png")
        return "离线", "已点击但未确认启动"
    return "离线", "未找到开机按钮"

def main():
    log("=== MCServerHost 自动巡检启动 ===")

    if not MC_EMAIL or not MC_PASSWORD:
        log("❌ 未配置 MC_EMAIL / MC_PASSWORD")
        return

    sb_kwargs = {
        "uc": True,
        "headless": False,  # 配合 Xvfb 虚拟显示
        "ad_block": False,
    }

    with SB(**sb_kwargs) as sb:
        try:
            sb.driver.set_page_load_timeout(40)

            if not login(sb):
                shot = screenshot(sb, "mc_error.png")
                tg_send("🔴 <b>MCServerHost 登录失败</b>\n请检查凭据或 CF 拦截。", shot)
                return

            status, action = control_server(sb)

            # 在线 / 离线 / 未知 一律都发 TG
            now_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

            if "运行中" in status:
                emoji = "🟢"
            elif "启动中" in status:
                emoji = "🟡"
            elif "离线" in status:
                emoji = "🔴"
            else:
                emoji = "⚪"

            report = (
                f"📋 <b>MCServerHost 巡检报告</b>\n\n"
                f"{emoji} <b>实例电源：</b><code>{status}</code>\n"
                f"⚡ <b>巡检动作：</b><code>{action}</code>\n"
                f"⏰ <b>执行时间：</b><code>{now_str}</code>"
            )

            # 截图优先级：最终状态 > 仪表盘 > 无图
            pic = None
            if os.path.exists("mc_final.png"):
                pic = "mc_final.png"
            elif os.path.exists("mc_dashboard.png"):
                pic = "mc_dashboard.png"

            tg_send(report, pic)
            log(f"✅ 巡检完毕：{status}，已发送 TG 通知")

        except Exception as e:
            log(f"❌ 运行异常: {e}")
            shot = screenshot(sb, "mc_error.png")
            tg_send(f"🔴 <b>运行异常</b>\n\n<code>{str(e)[:500]}</code>", shot)

if __name__ == "__main__":
    main()
