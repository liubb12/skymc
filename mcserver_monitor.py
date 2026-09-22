#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# MCServerHost 自动监控与开机脚本 (修复登录定位 + 精美排版)
# ============================================================
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from seleniumbase import Driver

# --- 环境变量配置 ---
MC_EMAIL = os.environ.get("MC_EMAIL", "").strip()       
MC_PASSWORD = os.environ.get("MC_PASSWORD", "").strip() 
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

LOGIN_URL = "https://mcserverhost.com/login"
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

def login_and_bypass_cf(driver):
    """处理登录与 Cloudflare 验证"""
    print(f"🌐 正在访问登录页面...", flush=True)
    driver.get(LOGIN_URL)
    # 增加页面渲染等待时间
    time.sleep(5)

    if "login" not in driver.current_url.lower():
        try:
            login_btn = driver.find_element(By.XPATH, "//a[contains(text(), 'Login')]")
            login_btn.click()
            time.sleep(3)
        except:
            pass

    print("🔑 正在输入账号密码...", flush=True)
    try:
        # 【核心修复】：扩大匹配范围，囊括 name='username', id='username' 等可能的情况
        driver.type("input[name='email'], input[name='username'], input[id='email'], input[id='username'], input[type='email']", MC_EMAIL)
        driver.type("input[type='password'], input[name='password'], input[id='password']", MC_PASSWORD)
        time.sleep(1)
        
        cf_frames = driver.find_elements(By.CSS_SELECTOR, "iframe[src*='challenges.cloudflare.com']")
        if cf_frames:
            print("🛡️ 检测到 Cloudflare 验证码，尝试通过...", flush=True)
            driver.uc_gui_click_captcha()
            time.sleep(3)

        # 点击登录按钮
        driver.click("button[type='submit'], button:contains('Sign in')")
        time.sleep(5)
    except Exception as e:
        print(f"❌ 登录表单交互失败: {e}", flush=True)
        return False

    if "login" not in driver.current_url.lower():
        print("✅ 登录成功！", flush=True)
        return True
    return False

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
    print("=== MCServerHost 自动监控巡检启动 ===", flush=True)
    
    if not MC_EMAIL or not MC_PASSWORD:
        print("❌ 未配置 MC_EMAIL 或 MC_PASSWORD 环境变量！", flush=True)
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

        # 1. 登录
        if not login_and_bypass_cf(driver):
            capture_screenshot(driver, "mc_error.png")
            tg_send("🔴 <b>MCServerHost 登录失败</b>\n请检查凭据或 Cloudflare 拦截情况。", "mc_error.png")
            return

        # 2. 检查与控制
        status, action = check_and_start_server(driver)
            
        # 3. 发送报告
        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        report = (
            f"📋 <b>MCServerHost 实例巡检报告</b>\n\n"
            f"🔑 <b>认证方式：</b><code>账号密码 自动登录</code>\n"
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
