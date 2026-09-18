#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Vektal Nodes 自动续期脚本 (完美铺满无黑边版)
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
    """仅捕获浏览器窗口视口，杜绝桌面背景黑边"""
    try:
        # 优先使用视口精准截图
        driver.save_screenshot(save_path)
        if os.path.exists(save_path) and os.path.getsize(save_path) > 15000:
            print(f"  📸 视口精准截屏成功 ({os.path.getsize(save_path)} 字节)")
            return True
    except Exception:
        pass

    # 兜底：使用 scrot
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


def main():
    print("=== Vektal Nodes 自动续期测试启动 ===", flush=True)

    # 紧凑视口：匹配该面板网页的卡片宽度，避免右侧多余空白
    chromium_args = [
        "--window-size=1080,800",
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

        time.sleep(4)

        # 2. 直达真实续期页面
        print(f"🚀 直达真实续期页面: {RENEWAL_COSTS_URL} ...", flush=True)
        driver.get(RENEWAL_COSTS_URL)

        # 等候卡片渲染
        print("⏳ 等待续期面板卡片渲染 (最多 15 秒)...", flush=True)
        for sec in range(15):
            time.sleep(1)
            body = driver.get_text("body")
            if "Restore your server" in body or "Next renewal" in body:
                print(f"  ✅ 续期卡片在第 {sec + 1} 秒就绪！")
                break

        # 3. 读取当前状态与周期倒计时
        server_state, remaining = get_server_status(driver)
        print(f"📊 当前状态: {server_state} | 周期剩余: {remaining}")

        # 4. 检查续期/恢复按钮并执行点击
        renew_executed = False
        action_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., 'Check renewal now') or contains(., 'Renew') or contains(., 'Restore')]"
        )
        for b in action_btns:
            if b.is_displayed() and b.is_enabled():
                btn_name = b.text.strip()
                print(f"🎯 命中操作按钮: [{btn_name}]，执行点击...")
                physical_click(driver, b)
                time.sleep(4)
                renew_executed = True
                break

        # 5. 精准视口截图（消除外部黑色桌面背景）
        time.sleep(2)
        capture_screenshot_smart(driver, "vektal_result.png")

        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        result_msg = "✅ 触发续期/检测成功" if renew_executed else "ℹ️ 周期未到期，无需操作"

        tg_send(
            f"📋 <b>Vektal Nodes 续期报告</b>\n\n"
            f"🔑 <b>登录结果：</b><code>成功</code>\n"
            f"🖥️ <b>服务器状态：</b><code>{server_state}</code>\n"
            f"⏳ <b>周期剩余：</b><code>{remaining}</code>\n"
            f"📊 <b>续期动作：</b><code>{result_msg}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>",
            photo_path="vektal_result.png"
        )
        print("✅ 测试执行完毕！")

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}")
        capture_screenshot_smart(driver, "error.png")
        tg_send(f"🔴 <b>Vektal 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="error.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
