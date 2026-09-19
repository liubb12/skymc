#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Vektal Nodes 自动续期 + 翼龙控制台开关机检测与自动开机
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
            print(f"  📸 视口精准截屏成功 ({os.path.getsize(save_path)} 字节)")
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


def login_with_credentials(driver, email, password) -> bool:
    print(f"🌐 正在打开登录页面: {LOGIN_URL} ...", flush=True)
    driver.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    time.sleep(4)

    # 填写账号
    print(f"📧 填写账号: {email} ...", flush=True)
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

    # 填写密码
    print("🔑 填写密码...", flush=True)
    pw_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='password'], input[name='password'], #password")
    for el in pw_inputs:
        if el.is_displayed():
            el.clear()
            el.send_keys(password)
            print("  ✅ 密码输入成功")
            break

    time.sleep(1)

    # 勾选服务条款复选框
    print("☑️ 寻找并勾选服务条款复选框...", flush=True)
    boxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    for box in boxes:
        try:
            driver.execute_script("arguments[0].click();", box)
            print("  ✅ 已通过 JS 勾选 Checkbox")
            break
        except Exception:
            pass

    time.sleep(1)

    # 点击 Sign in
    print("👉 查找并点击 Sign in 按钮...", flush=True)
    sign_in_btns = driver.find_elements(
        By.XPATH,
        "//button[contains(., 'Sign in') or contains(., 'Sign In') or @type='submit']"
    )
    for b in sign_in_btns:
        if b.is_displayed():
            physical_click(driver, b)
            print("  ✅ 已点击 Sign in 提交按钮")
            break

    # 等待登录成功跳转
    print("⏳ 等待跳转控制台并确认页面加载...", flush=True)
    for _ in range(20):
        time.sleep(2)
        url = driver.current_url.lower()
        if "login" not in url:
            print(f"  🎉 登录成功！当前页面: {driver.current_url}", flush=True)
            return True
        try:
            driver.uc_gui_click_captcha()
        except Exception:
            pass

    return False


def get_server_status(driver):
    body = driver.get_text("body").replace("\u00a0", " ")
    remaining_text = "未知"

    m = re.search(r"Next renewal\s+([0-9a-zA-Z\s]+?remaining)", body, re.IGNORECASE)
    if m:
        remaining_text = m.group(1).strip()
    else:
        m2 = re.search(r"(\d+\s*d(?:ays?)?\s*\d+\s*h(?:ours?)?)\s*remaining", body, re.IGNORECASE)
        if m2:
            remaining_text = m2.group(0).strip()
        elif "48 hours" in body:
            remaining_text = "48 hours 周期中"

    server_state = "active (运行中)"
    try:
        state_elements = driver.find_elements(
            By.XPATH,
            "//*[contains(text(), 'SERVER STATE')]/following::*[1] | //*[contains(@class, 'badge') or contains(@class, 'status')]"
        )
        for el in state_elements:
            txt = el.text.strip().lower()
            if "suspended" in txt:
                server_state = "suspended (已挂起)"
                break
            elif "active" in txt:
                server_state = "active (运行中)"
                break
    except Exception:
        pass

    return server_state, remaining_text


def check_and_start_pterodactyl_server(driver):
    """
    进入翼龙后台 (Pterodactyl Panel) 检查 Minecraft 实例开机状态：
    若离线 (Offline) 则自动点击绿色播放键开机。
    """
    print(f"🚀 前往翼龙面板: {PANEL_BASE_URL} ...", flush=True)
    driver.get(PANEL_BASE_URL)
    time.sleep(4)

    # 1. 如果在服务器列表页面，点击第一台服务器卡片
    if "/server/" not in driver.current_url:
        print("🔍 查找服务器卡片 (如 myindf)...", flush=True)
        server_cards = driver.find_elements(
            By.XPATH,
            "//a[contains(@href, '/server/')] | //div[contains(., 'myindf') and @role='button'] | //*[contains(text(), 'myindf')]/ancestor::a"
        )
        if not server_cards:
            # 尝试通过页面内带链接的卡片定位
            server_cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/server/']")

        if server_cards:
            print(f"  👉 点击进入服务器控制台: {server_cards[0].text.strip() or 'Server'}")
            physical_click(driver, server_cards[0])
            time.sleep(5)
        else:
            print("  ⚠️ 未在列表检测到服务器，尝试直连或者已在当前页...")

    # 2. 等待控制台仪表盘加载
    time.sleep(3)
    current_body = driver.get_text("body")

    # 3. 提取运行状态 (Offline / Running / Starting)
    power_state = "未知"
    if "Offline" in current_body:
        power_state = "Offline (已关机)"
    elif "Starting" in current_body:
        power_state = "Starting (启动中)"
    elif "Running" in current_body:
        power_state = "Running (运行中)"
    else:
        # 正则检查 Uptime 状态
        m = re.search(r"Uptime\s*([A-Za-z0-9]+)", current_body)
        if m:
            power_state = m.group(1).strip()

    print(f"🖥️ 翼龙服务器当前电源状态: {power_state}", flush=True)

    power_action = "无需操作"
    # 4. 如果是离线状态，触发开机
    if "offline" in power_state.lower():
        print("⚡ 服务器当前处于关机状态，正在寻找绿色开机按钮 (Start)...", flush=True)
        # 定位绿色播放三角按钮或包含 Start 的操作按钮
        start_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(@class, 'bg-green') or contains(@class, 'success') or @aria-label='Start Server' or contains(., 'Start')] | //button[./*[name()='svg'] and contains(@class, 'green')]"
        )
        # 兜底查找顶部操作栏所有按钮中的第一个（开机键在最左）
        if not start_btns:
            top_btns = driver.find_elements(By.XPATH, "//div[contains(@class, 'rounded')]//button")
            if top_btns:
                start_btns = [top_btns[0]]

        if start_btns:
            print("🎯 命中开机按钮，执行点击开机...")
            physical_click(driver, start_btns[0])
            time.sleep(5)
            power_action = "⚡ 已执行开机操作"
            power_state = "Starting (启动中)"
        else:
            print("⚠️ 未能精准定位到 Start 按钮，尝试通过快捷键或通用按键触发。")
            power_action = "⚠️ 未找到开机按钮"
    else:
        print("✅ 服务器运行正常，保持运行。")

    return power_state, power_action


def main():
    print("=== Vektal Nodes 自动续期 + 开关机巡检启动 ===", flush=True)

    chromium_args = [
        "--window-size=1200,900",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))

    try:
        # 1. 账号密码登录
        if not login_with_credentials(driver, VEKTAL_EMAIL, VEKTAL_PASSWORD):
            capture_screenshot_smart(driver, "login_failed.png")
            tg_send("🔴 <b>Vektal Nodes 登录失败</b>", photo_path="login_failed.png")
            return

        time.sleep(3)

        # 2. 直达续期页面，执行 48h 周期续期检测
        print(f"🚀 直达续期页面: {RENEWAL_COSTS_URL} ...", flush=True)
        driver.get(RENEWAL_COSTS_URL)

        for sec in range(15):
            time.sleep(1)
            body = driver.get_text("body")
            if "Restore your server" in body or "Next renewal" in body:
                break

        server_state, remaining = get_server_status(driver)
        print(f"📊 周期状态: {server_state} | 周期剩余: {remaining}")

        renew_executed = False
        action_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'Check renewal now') or contains(., 'Renew') or contains(., 'Restore')]"
        )
        for b in action_btns:
            if b.is_displayed() and b.is_enabled():
                print(f"🎯 命中续期按钮: [{b.text.strip()}]，执行点击...")
                physical_click(driver, b)
                time.sleep(4)
                renew_executed = True
                break

        renew_msg = "✅ 触发续期/检测成功" if renew_executed else "ℹ️ 周期未到期，无需续期"

        # 3. 巡检翼龙控制台开关机状态
        power_state, power_action = check_and_start_pterodactyl_server(driver)

        # 4. 在控制台截取最终仪表盘（包含电源状态、内存、CPU等）
        time.sleep(2)
        capture_screenshot_smart(driver, "vektal_result.png")

        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

        tg_send(
            f"📋 <b>Vektal Nodes 巡检报告</b>\n\n"
            f"🔑 <b>登录结果：</b><code>成功</code>\n"
            f"⏳ <b>续期周期：</b><code>{remaining}</code>\n"
            f"🔄 <b>续期动作：</b><code>{renew_msg}</code>\n"
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
