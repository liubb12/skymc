#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# MCServerHost 自动监控与开机脚本 (终极指纹伪装破盾版)
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

def challenge_visible(sb):
    try:
        src = sb.get_page_source()
        return ("Verify you are human" in src) or ("Security Verification" in src) or ("Please complete the captcha verification" in src)
    except Exception:
        return False

def handle_cloudflare(sb, max_retry=5):
    """反复轰炸式点盾"""
    if not challenge_visible(sb):
        return True
    print("🛡️ 检测到 Cloudflare 盾，尝试突围...", flush=True)
    for i in range(max_retry):
        try:
            sb.uc_gui_click_captcha()
            time.sleep(5)
            if not challenge_visible(sb):
                print("   ✅ CF 验证已通过！", flush=True)
                return True
        except Exception:
            pass
        # 补救点击法
        try:
            cf_frames = sb.find_elements("iframe[src*='challenges.cloudflare.com']")
            if cf_frames:
                ActionChains(sb.driver).move_to_element(cf_frames[0]).click().perform()
        except:
            pass
        time.sleep(3)
    return False

def login_and_bypass_cf(sb):
    print(f"🌐 正在以极隐蔽模式访问登录页面...", flush=True)
    
    # 核心伪装 1：用极慢的加载和重连来伪装成家用烂网络
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
    time.sleep(5)
    
    # 清理初始可能存在的盾
    handle_cloudflare(sb, max_retry=3)

    if "login" not in sb.get_current_url().lower():
        try:
            sb.click('a:contains("Login")')
            time.sleep(4)
        except:
            pass

    print("🔑 正在像真人一样缓慢输入账号密码...", flush=True)
    try:
        email_selectors = "input[name='email'], input[name='username'], input[type='email']"
        pass_selectors = "input[type='password'], input[name='password']"
        
        sb.wait_for_element_visible(email_selectors, timeout=10)
        
        # 核心伪装 2：清除内容，然后缓慢输入（模拟真人键盘敲击），坚决不用直接赋值
        sb.clear(email_selectors)
        sb.type(email_selectors, MC_EMAIL)
        time.sleep(1)
        
        sb.clear(pass_selectors)
        sb.type(pass_selectors, MC_PASSWORD)
        time.sleep(1.5)
        
        # 用 JS 强行勾选
        try:
            sb.execute_script("document.querySelector('input[type=\"checkbox\"]').checked = true;")
        except:
            pass
            
        time.sleep(1)

        print("💥 准备发起登录冲击...", flush=True)
        login_btn_selectors = "button[type='submit'], button:contains('Sign in')"
        
        # 尝试暴力点击
        for i in range(3):
            try:
                sb.uc_click(login_btn_selectors)
            except:
                try:
                    sb.click(login_btn_selectors)
                except:
                    pass
            time.sleep(3)
            
            # 点击后极大概率出盾，疯狂点盾
            if challenge_visible(sb):
                handle_cloudflare(sb, max_retry=4)
                
            if "login" not in sb.get_current_url().lower():
                break

    except Exception as e:
        print(f"❌ 登录表单交互失败: {e}", flush=True)
        return False

    if "login" not in sb.get_current_url().lower():
        print(f"✅ 成功杀入后台！当前 URL: {sb.get_current_url()}", flush=True)
        return True
    return False

def check_and_start_server(sb):
    """检查服务器状态并执行开机"""
    print(f"📡 正在获取服务器列表信息...", flush=True)
    try:
        sb.uc_open_with_reconnect(SERVERS_URL, reconnect_time=4)
    except:
        sb.open(SERVERS_URL)
        
    time.sleep(5)
    handle_cloudflare(sb, max_retry=2)

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

    if "唤醒" in action_taken:
        sb.driver.refresh()
        time.sleep(5)
        final_text = sb.get_text("body").lower()
        if "running" in final_text or "starting" in final_text:
            server_status = "启动中 / 运行中"
        safe_screenshot(sb, "mc_final_status.png")
        
    return server_status, action_taken

def main():
    print("=== MCServerHost 自动监控巡检启动 (终极指纹伪装版) ===", flush=True)
    
    if not MC_EMAIL or not MC_PASSWORD:
        print("❌ 未配置 MC_EMAIL 或 MC_PASSWORD 环境变量！", flush=True)
        return

    # 极净模式伪装参数
    sb_kwargs = {
        "uc": True, 
        "headless": False, 
        "locale_code": "en",
        "agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" # 强制写死一个极高信誉的常用 UA
    }
    
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
