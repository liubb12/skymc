#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Vektal Nodes 自动续期 + 翼龙控制台自动登录与开关机巡检 (精准适配 Renew for FREE 版)
# ============================================================
import html
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
import requests
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from seleniumbase import Driver

BASE_URL = "https://vektalnodes.in"
LOGIN_URL = f"{BASE_URL}/login"
RENEWAL_COSTS_URL = f"{BASE_URL}/renewal-costs"
PANEL_BASE_URL = "https://panel.vektalnodes.in"
SERVER_CONSOLE_URL = "https://panel.vektalnodes.in/server/8a709478"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

VEKTAL_EMAIL = os.environ.get("VEKTAL_EMAIL", "").strip() or os.environ.get("VEKTAL_USERNAME", "").strip()
VEKTAL_PASSWORD = os.environ.get("VEKTAL_PASSWORD", "").strip()


def tg_send(text: str, photo_path: str = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过通知。")
        return
    try:
        if photo_path and os.path.exists(photo_path) and os.path.getsize(photo_path) > 1000:
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
        print("  ✅ TG 通知发送成功")
    except Exception as e:
        print(f"  ⚠️ TG 通知异常: {e}")


def capture_screenshot_smart(driver, save_path="vektal_result.png"):
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


def robust_click(driver, element):
    """复合穿透点击，确保按钮被真实触发"""
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
        driver.execute_script("""
            var el = arguments[0];
            var opts = { bubbles: true, cancelable: true, view: window };
            el.dispatchEvent(new MouseEvent('mousedown', opts));
            el.dispatchEvent(new MouseEvent('mouseup', opts));
            el.dispatchEvent(new MouseEvent('click', opts));
        """, element)
    except Exception:
        element.click()


def login_main_site(driver, email, password) -> bool:
    print(f"🌐 正在打开主控登录页面: {LOGIN_URL} ...", flush=True)
    driver.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    time.sleep(4)

    if "login" not in driver.current_url.lower():
        print("🎉 检测到已有登录会话，无需重复登录！", flush=True)
        return True

    print(f"📧 填写主控账号: {email} ...", flush=True)
    email_inputs = driver.find_elements(
        By.CSS_SELECTOR,
        "input[type='email'], input[name='email'], input[name='username'], #email, #username"
    )
    for el in email_inputs:
        if el.is_displayed():
            el.clear()
            el.send_keys(email)
            print("  ✅ 账号输入成功")
            break

    print("🔑 填写密码...", flush=True)
    pw_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='password'], #password")
    for el in pw_inputs:
        if el.is_displayed():
            el.clear()
            el.send_keys(password)
            print("  ✅ 密码输入成功")
            break

    time.sleep(1)

    boxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    for box in boxes:
        try:
            driver.execute_script("arguments[0].click();", box)
            print("  ✅ 已勾选 Checkbox")
            break
        except Exception:
            pass

    time.sleep(1)

    sign_in_btns = driver.find_elements(
        By.XPATH,
        "//button[contains(., 'Sign in') or contains(., 'Sign In') or @type='submit']"
    )
    for b in sign_in_btns:
        if b.is_displayed():
            robust_click(driver, b)
            print("  ✅ 已点击 Sign in 提交按钮")
            break

    print("⏳ 等待跳转控制台...", flush=True)
    for _ in range(15):
        time.sleep(2)
        url = driver.current_url.lower()
        if "login" not in url:
            print(f"  🎉 主站登录成功！当前页面: {driver.current_url}", flush=True)
            return True
        try:
            driver.uc_gui_click_captcha()
        except Exception:
            pass

    return False


def get_server_status(driver):
    body = driver.get_text("body").replace("\u00a0", " ")
    remaining_text = "未知"

    # 优先匹配当前页面的恢复倒计时：Renewal window ends 1d 23h remaining. 或 1d 23h remaining
    m_rem = re.search(r"(?:Renewal window ends|RECOVERY WINDOW)\s*([\d\w\s]+?remaining)", body, re.IGNORECASE)
    if m_rem:
        remaining_text = m_rem.group(1).strip()
    else:
        m = re.search(r"Next renewal\s+([0-9a-zA-Z\s]+?remaining)", body, re.IGNORECASE)
        if m:
            remaining_text = m.group(1).strip()
        else:
            m2 = re.search(r"(\d+\s*[dhms]\s*)+remaining", body, re.IGNORECASE)
            if m2:
                remaining_text = m2.group(0).strip()
            elif "48-hour" in body:
                remaining_text = "48小时恢复周期活跃中"

    server_state = "active (周期运行中)"
    if "SERVER STATE" in body:
        m_state = re.search(r"SERVER STATE[\s\n\r]+([a-zA-Z]+)", body)
        if m_state:
            val = m_state.group(1).strip().lower()
            if "suspended" in val:
                server_state = "suspended (已挂起/待恢复)"
            elif "active" in val:
                server_state = "active (周期运行中)"
    elif "suspended" in body.lower() and "Restore your server" in body:
        server_state = "suspended (已挂起/待恢复)"

    return server_state, remaining_text


def check_and_start_pterodactyl_server(driver, email, password):
    """处理翼龙控制台登录并开机巡检"""
    print(f"🚀 前往翼龙面板控制台: {SERVER_CONSOLE_URL} ...", flush=True)
    driver.get(SERVER_CONSOLE_URL)
    time.sleep(4)

    current_body = driver.get_text("body")
    if "Login to Continue" in current_body or "login" in driver.current_url.lower():
        print("🔐 检测到翼龙面板需要登录，正在填入凭据...", flush=True)
        p_emails = driver.find_elements(By.CSS_SELECTOR, "input[type='text'], input[type='email'], input[name='username']")
        for el in p_emails:
            if el.is_displayed():
                el.clear()
                el.send_keys(email)
                print("  ✅ 翼龙账号输入成功")
                break

        p_pws = driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='password']")
        for el in p_pws:
            if el.is_displayed():
                el.clear()
                el.send_keys(password)
                print("  ✅ 翼龙密码输入成功")
                break

        time.sleep(1)

        p_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Login') or @type='submit']")
        for b in p_btns:
            if b.is_displayed():
                robust_click(driver, b)
                print("  👉 已点击翼龙 Login 按钮")
                break

        time.sleep(5)
        driver.get(SERVER_CONSOLE_URL)
        time.sleep(5)

    current_body = driver.get_text("body")

    power_state = "未知"
    if "Offline" in current_body:
        power_state = "Offline (已关机)"
    elif "Starting" in current_body:
        power_state = "Starting (启动中)"
    elif "Running" in current_body:
        power_state = "Running (运行中)"
    else:
        m = re.search(r"Uptime\s*([A-Za-z0-9]+)", current_body)
        if m:
            power_state = f"Running (Uptime: {m.group(1).strip()})"

    print(f"🖥️ 翼龙服务器当前电源状态: {power_state}", flush=True)

    power_action = "无需操作"
    if "offline" in power_state.lower():
        print("⚡ 服务器已离线，正在寻找绿色开机按钮 (Start)...", flush=True)
        start_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(@class, 'bg-green') or contains(@class, 'success') or @aria-label='Start Server' or contains(., 'Start')] | //button[./*[name()='svg'] and contains(@class, 'green')]"
        )
        if not start_btns:
            top_btns = driver.find_elements(By.XPATH, "//div[contains(@class, 'flex')]//button")
            if top_btns:
                start_btns = [top_btns[0]]

        if start_btns:
            print("🎯 命中开机按钮，执行开机操作...")
            robust_click(driver, start_btns[0])
            time.sleep(4)
            power_action = "⚡ 已执行开机操作"
            power_state = "Starting (启动中)"
        else:
            print("⚠️ 未定位到 Start 按钮")
            power_action = "⚠️ 未找到开机按钮"
    else:
        print("✅ 服务器运行正常，保持在线。")

    return power_state, power_action


def main():
    print("=== Vektal Nodes 自动续期 + 开关机巡检启动 ===", flush=True)

    chromium_args = [
        "--window-size=1440,900",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))

    try:
        # 1. 主站登录
        if not login_main_site(driver, VEKTAL_EMAIL, VEKTAL_PASSWORD):
            capture_screenshot_smart(driver, "login_failed.png")
            tg_send("🔴 <b>Vektal Nodes 登录失败</b>", photo_path="login_failed.png")
            return

        time.sleep(2)

        # 2. 续期页面巡检
        print(f"🚀 直达续期页面: {RENEWAL_COSTS_URL} ...", flush=True)
        driver.get(RENEWAL_COSTS_URL)

        for _ in range(10):
            time.sleep(1)
            body = driver.get_text("body")
            if "Restore your server" in body or "Next renewal" in body or "Renew for FREE" in body:
                break

        server_state, remaining = get_server_status(driver)
        print(f"📊 周期状态: {server_state} | 周期剩余: {remaining}")

        renew_executed = False
        action_desc = "ℹ️ 周期未结束，无需操作"

        # 精准匹配：无论是 suspended 还是直接出现了带有 Renew for FREE 的按钮
        if "suspended" in server_state.lower() or "Renew for FREE" in driver.get_text("body"):
            print("🔍 正在定位 Renew for FREE 橙色续期按钮...", flush=True)
            action_btns = driver.find_elements(
                By.XPATH,
                "//button[contains(., 'Renew for FREE') or contains(., 'Restore') or contains(., 'Renew Server')] | "
                "//*[contains(@class, 'button') or @role='button'][contains(., 'Renew for FREE')] | "
                "//button[contains(@class, 'orange') or contains(@class, 'red') or contains(., 'Renew')]"
            )
            for b in action_btns:
                try:
                    if b.is_displayed() and b.is_enabled():
                        btn_txt = b.text.strip()
                        print(f"🎯 命中目标按钮: [{btn_txt}]，执行点击...")
                        robust_click(driver, b)
                        time.sleep(5)
                        renew_executed = True
                        action_desc = f"✅ 成功触发续期/恢复 ({btn_txt})"
                        break
                except Exception:
                    continue

        # 3. 进入翼龙控制台检测电源与开机（刚续完期翼龙一般会处于 Offline 状态，正好将其开机）
        power_state, power_action = check_and_start_pterodactyl_server(driver, VEKTAL_EMAIL, VEKTAL_PASSWORD)

        # 4. 截图并发送报告
        time.sleep(2)
        capture_screenshot_smart(driver, "vektal_result.png")

        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

        tg_send(
            f"📋 <b>Vektal Nodes 巡检报告</b>\n\n"
            f"🔑 <b>主站登录：</b><code>成功</code>\n"
            f"📊 <b>周期状态：</b><code>{server_state}</code>\n"
            f"⏳ <b>续期周期：</b><code>{remaining}</code>\n"
            f"🔄 <b>续期动作：</b><code>{action_desc}</code>\n"
            f"🖥️ <b>实例电源：</b><code>{power_state}</code>\n"
            f"⚡ <b>开机动作：</b><code>{power_action}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>",
            photo_path="vektal_result.png"
        )
        print("✅ 全部巡检任务执行完毕！")

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}")
        capture_screenshot_smart(driver, "error.png")
        tg_send(f"🔴 <b>Vektal 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="error.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
