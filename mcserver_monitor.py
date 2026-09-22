#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# MCServerHost 自动监控与开机脚本 (Cookie 免登 + XSRF 过滤终极版)
# ============================================================
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from seleniumbase import Driver

# --- 环境变量配置 ---
MC_COOKIE = os.environ.get("MC_COOKIE", "").strip() 
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

BASE_URL = "https://mcserverhost.com"
SERVERS_URL = "https://mcserverhost.com/servers"

def tg_send(text: str, photo_path: str = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过通知。", flush=True)
        return
    try:
        if photo_path and os.path.exists(photo_path):
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
            with open(photo_path, "rb") as f:
                requests.post(
                    url,
                    data={"chat_id": TG_CHAT_ID, "caption": text, "parse_mode": "HTML"},
                    files={"photo": f},
                    timeout=30,
                )
        else:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
            requests.post(
                url,
                data={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=30,
            )
        print("✅ TG 通知发送成功", flush=True)
    except Exception as e:
        print(f"⚠️ TG 通知发送异常: {e}", flush=True)

def capture_screenshot(driver, save_path="mc_status.png"):
    try:
        driver.save_screenshot(save_path)
    except:
        pass

def inject_cookies_and_navigate(driver, raw_cookie_str: str) -> bool:
    print("🌐 正在初始化会话并注入 Cookie...", flush=True)
    driver.uc_open_with_reconnect(BASE_URL, reconnect_time=4)
    time.sleep(3)

    print("🍪 注入持久 Cookie 凭据 (自动过滤易冲突的 XSRF-TOKEN)...", flush=True)
    for item in raw_cookie_str.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, val = item.split("=", 1)
        name = name.strip()
        val = val.strip()

        # 【核心修复】：跳过旧的 XSRF-TOKEN，防止 Laravel 报 419 错误并注销会话
        if name.upper() == "XSRF-TOKEN":
            continue

        for dom in ["mcserverhost.com", ".mcserverhost.com"]:
            cookie_dict = {
                "name": name,
                "value": val,
                "domain": dom,
                "path": "/",
            }
            try:
                driver.add_cookie(cookie_dict)
            except Exception:
                pass

    print(f"🚀 直达目标控制台: {SERVERS_URL} ...", flush=True)
    driver.get(SERVERS_URL)
    time.sleep(5)

    # 419 异常防御与自动重载修复
    body_text = driver.get_text("body").upper()
    if "419" in body_text or "PAGE EXPIRED" in body_text:
        print("⚠️ 捕获到 419 页面过期，执行会话对齐硬刷新...", flush=True)
        driver.refresh()
        time.sleep(6)

    if "login" in driver.current_url.lower():
        print("❌ Cookie 凭据失效，当前停留在登录页！", flush=True)
        return False

    print("✅ Cookie 注入成功，已彻底跳过 CF 登录验证！", flush=True)
    return True

def check_and_start_server(driver):
    """检查服务器状态并执行开机"""
    print(f"📡 正在获取服务器列表信息...", flush=True)
    driver.get(SERVERS_URL)
    time.sleep(5)

    capture_screenshot(driver, "mc_dashboard.png")
    page_text = driver.get_text("body").lower()
    
    server_status = "未知状态"
    action_taken = "检测失败"

    if "running" in page_text:
        server_status = "在线 (运行中)"
        action_taken = "无需操作 (已在运行)"
        print("🟢 服务器当前状态：运行中 (running)", flush=True)
    elif "offline" in page_text or "stopped" in page_text:
        server_status = "离线 (已关机)"
        print("🔴 服务器当前状态：已关机 (offline)，准备执行开机...", flush=True)
        
        try:
            play_buttons = driver.find_elements(By.XPATH, "//svg[contains(@class, 'play') or @data-lucide='play']/ancestor::button | //button[.//svg]")
            for btn in play_buttons:
                if btn.is_displayed():
                    driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", btn)
                    time.sleep(1)
                    btn.click()
                    print("⚡ 成功点击【开机】按钮！", flush=True)
                    action_taken = "执行唤醒 (发送开机指令)"
                    time.sleep(5)
                    break
        except Exception as e:
            print(f"⚠️ 点击开机按钮时发生异常: {e}", flush=True)
            action_taken = "开机失败 (未找到控制按钮)"
    else:
        print("⚠️ 未能明确识别服务器状态，请检查截图。", flush=True)

    # 如果执行了开机，刷新页面确认最终状态
    if "唤醒" in action_taken:
        driver.refresh()
        time.sleep(5)
        final_text = driver.get_text("body").lower()
        if "running" in final_text or "starting" in final_text:
            server_status = "启动中 / 运行中"
        capture_screenshot(driver, "mc_final_status.png")
        
    return server_status, action_taken

def main():
    print("=== MCServerHost 自动监控巡检启动 (Cookie 免登版) ===", flush=True)
    
    if not MC_COOKIE:
        print("❌ 未配置 MC_COOKIE 环境变量，请在 Secrets 中添加！", flush=True)
        return

    chromium_args = [
        "--window-size=1920,1080",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    
    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))
    
    try:
        time.sleep(3)
        driver.set_page_load_timeout(30)

        # 1. 注入 Cookie 绕过登录
        if not inject_cookies_and_navigate(driver, MC_COOKIE):
            capture_screenshot(driver, "mc_error.png")
            tg_send("🔴 <b>MCServerHost 登录失败</b>\nCookie 已失效，请在浏览器中重新抓取并更新 GitHub Secrets。", "mc_error.png")
            return

        # 2. 检查与控制
        status, action = check_and_start_server(driver)
            
        # 【静默巡检】：如果是正常运行，直接退出，不发消息
        if "运行中" in status and "无需操作" in action:
            print("✅ 巡检完毕：服务器正常运行中，脚本静默退出，不发送打扰通知。", flush=True)
            return

        # 3. 发送排版精美的报告
        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        report = (
            f"📋 <b>MCServerHost 实例巡检报告</b>\n\n"
            f"🔑 <b>认证方式：</b><code>Cookie 免登</code>\n"
            f"🎮 <b>服务类型：</b><code>Minecraft</code>\n"
            f"🖥️ <b>实例电源：</b><code>{status}</code>\n"
            f"⚡ <b>巡检动作：</b><code>{action}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>"
        )
        
        pic_to_send = "mc_final_status.png" if os.path.exists("mc_final_status.png") else "mc_dashboard.png"
        tg_send(report, pic_to_send)
        
        print("✅ 唤醒任务执行完毕！", flush=True)

    except Exception as e:
        print(f"❌ 运行异常: {e}", flush=True)
        capture_screenshot(driver, "mc_error.png")
        tg_send(f"🔴 <b>MCServerHost 运行异常</b>\n\n<code>{str(e)}</code>", "mc_error.png")
    finally:
        try:
            driver.quit()
        except:
            pass

if __name__ == "__main__":
    main()
