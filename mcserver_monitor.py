#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# MCServerHost 自动监控与开机脚本 (移植 SkyMC CF 破盾框架版)
# ============================================================
import os
import time
import requests
from datetime import datetime, timedelta, timezone
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from seleniumbase import SB

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


def safe_screenshot(sb, save_path="mc_status.png"):
    try:
        sb.save_screenshot(save_path)
    except:
        pass


# ==========================================
# 移植自 SkyMC 的 Cloudflare 破盾核心逻辑
# ==========================================
def challenge_visible(sb):
    try:
        src = sb.get_page_source()
        return ("Verify you are human" in src) or ("Security Verification" in src) or ("Please complete the captcha verification" in src)
    except Exception:
        return False

def handle_cloudflare(sb, max_retry=3):
    if not challenge_visible(sb):
        return True
    print("🛡️ 检测到 Cloudflare 人机验证弹窗，开始处理...", flush=True)
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


def login_and_bypass_cf(sb):
    """处理登录与 Cloudflare 验证"""
    print(f"🌐 正在访问登录页面...", flush=True)
    try:
        # 使用 UC 模式特有的断线重连加载法，抹除 webdriver 指纹
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    except Exception:
        sb.open(LOGIN_URL)
        
    sb.wait_for_ready_state_complete()
    time.sleep(4)

    handle_cloudflare(sb)

    if "login" not in sb.get_current_url().lower():
        try:
            sb.click('a:contains("Login")')
            time.sleep(3)
        except:
            pass

    print("🔑 正在输入账号密码...", flush=True)
    try:
        # 兼容各种账号输入框的命名
        email_selectors = "input[name='email'], input[name='username'], input[type='email']"
        pass_selectors = "input[type='password'], input[name='password']"
        
        sb.wait_for_element_visible(email_selectors, timeout=10)
        sb.type(email_selectors, MC_EMAIL)
        sb.type(pass_selectors, MC_PASSWORD)
        time.sleep(1)
        
        # 勾选 Remember me 
        try:
            print("✅ 尝试勾选 Remember me...", flush=True)
            sb.execute_script("""
                var cb = document.querySelector('input[type="checkbox"]');
                if(cb && !cb.checked) { cb.click(); }
            """)
        except:
            pass
            
        time.sleep(1)

        # 点击前先看看有没有盾
        handle_cloudflare(sb)

        print("🔑 尝试点击登录按钮...", flush=True)
        login_btn_selectors = "button[type='submit'], button:contains('Sign in')"
        try:
            sb.uc_click(login_btn_selectors)
        except:
            sb.click(login_btn_selectors)
            
        time.sleep(5)
        
        # 登录后死守拦截验证码
        wait_challenge_gone(sb, timeout=15)

    except Exception as e:
        print(f"❌ 登录表单交互失败: {e}", flush=True)
        return False

    if "login" not in sb.get_current_url().lower():
        print("✅ 登录成功！", flush=True)
        return True
    return False


def check_and_start_server(sb):
    """检查服务器状态并执行开机"""
    print(f"📡 正在获取服务器列表信息...", flush=True)
    sb.open(SERVERS_URL)
    sb.wait_for_ready_state_complete()
    time.sleep(5)
    handle_cloudflare(sb)

    safe_screenshot(sb, "mc_dashboard.png")
    page_text = sb.get_text("body").lower()
    
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
            play_buttons = sb.find_elements("//svg[contains(@class, 'play') or @data-lucide='play']/ancestor::button | //button[.//svg]")
            for btn in play_buttons:
                if btn.is_displayed():
                    sb.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", btn)
                    time.sleep(1)
                    ActionChains(sb.driver).move_to_element(btn).click().perform()
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
        sb.driver.refresh()
        time.sleep(5)
        final_text = sb.get_text("body").lower()
        if "running" in final_text or "starting" in final_text:
            server_status = "启动中 / 运行中"
        safe_screenshot(sb, "mc_final_status.png")
        
    return server_status, action_taken


def main():
    print("=== MCServerHost 自动监控巡检启动 (SkyMC 破盾移植版) ===", flush=True)
    
    if not MC_EMAIL or not MC_PASSWORD:
        print("❌ 未配置 MC_EMAIL 或 MC_PASSWORD 环境变量！", flush=True)
        return

    sb_kwargs = {
        "uc": True, 
        "headless": False, 
        "locale_code": "en",
    }
    
    # 采用 SkyMC 中的 SB 上下文管理器，确保驱动彻底隔离和清理
    with SB(**sb_kwargs) as sb:
        try:
            sb.driver.set_window_size(1920, 1080)
            sb.driver.set_page_load_timeout(30)

            # 1. 登录
            if not login_and_bypass_cf(sb):
                safe_screenshot(sb, "mc_error.png")
                tg_send("🔴 <b>MCServerHost 登录失败</b>\n请检查凭据或 Cloudflare 拦截情况。", "mc_error.png")
                return

            # 2. 检查与控制
            status, action = check_and_start_server(sb)
            
            # 【免打扰判断】：如果无需操作，则静默退出
            if "运行中" in status and "无需操作" in action:
                print("✅ 巡检完毕：服务器正常运行中，脚本静默退出，不发送打扰通知。", flush=True)
                return
                
            # 3. 发送排版精美的报告
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
            safe_screenshot(sb, "mc_error.png")
            tg_send(f"🔴 <b>MCServerHost 运行异常</b>\n\n<code>{str(e)}</code>", "mc_error.png")

if __name__ == "__main__":
    main()
