#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# Vektal Nodes 账号密码登录与续期测试脚本 (带条款自动勾选版)
# ============================================================
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
import requests
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from seleniumbase import Driver

BASE_URL = "https://vektalnodes.in"
LOGIN_URL = f"{BASE_URL}/login"
DASHBOARD_URL = f"{BASE_URL}/dashboard"
RENEW_PAGE_URL = f"{BASE_URL}/dashboard/renew"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

# 账号与密码
VEKTAL_EMAIL = os.environ.get("VEKTAL_EMAIL", "").strip() or os.environ.get("VEKTAL_USERNAME", "").strip()
VEKTAL_PASSWORD = os.environ.get("VEKTAL_PASSWORD", "").strip()


def tg_send(text: str, photo_path: str = None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ 未配置 TG_BOT_TOKEN / TG_CHAT_ID，跳过通知。")
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
        print("  ✅ TG 通知发送成功")
    except Exception as e:
        print(f"  ⚠️ TG 通知异常: {e}")


def physical_click(driver, element):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        time.sleep(0.2)
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(element).pause(0.1).click().perform()
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

    # 1. 检测并尝试处理前置 Cloudflare 盾
    try:
        driver.uc_gui_click_captcha()
        time.sleep(2)
    except Exception:
        pass

    # 2. 填写账号/邮箱
    print(f"📧 填写账号: {email} ...", flush=True)
    email_inputs = driver.find_elements(
        By.CSS_SELECTOR,
        "input[type='email'], input[name='email'], input[name='username'], #email, #username, input[placeholder*='email' i], input[placeholder*='username' i]"
    )
    email_filled = False
    for el in email_inputs:
        if el.is_displayed():
            el.clear()
            el.send_keys(email)
            email_filled = True
            print("  ✅ 账号输入成功")
            break

    if not email_filled:
        print("  ⚠️ 未能直接定位到可见邮箱输入框，尝试 JS 输入")
        driver.execute_script("""
            const el = document.querySelector("input[type='email'], input[name='email'], input[name='username']");
            if(el) { el.value = arguments[0]; el.dispatchEvent(new Event('input', {bubbles: true})); }
        """, email)

    # 3. 填写密码
    print("🔑 填写密码...", flush=True)
    pw_inputs = driver.find_elements(
        By.CSS_SELECTOR,
        "input[type='password'], input[name='password'], #password"
    )
    for el in pw_inputs:
        if el.is_displayed():
            el.clear()
            el.send_keys(password)
            print("  ✅ 密码输入成功")
            break

    time.sleep(1)

    # 4. 关键：勾选服务条款/年龄复选框
    print("☑️ 寻找并勾选服务条款复选框...", flush=True)
    checked = False

    # 优先原生 input checkbox
    boxes = driver.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    for box in boxes:
        try:
            if not box.is_selected():
                driver.execute_script("arguments[0].click();", box)
                print("  ✅ 已通过 JS 勾选 input[type='checkbox']")
                checked = True
                break
            else:
                checked = True
                break
        except Exception:
            pass

    # 若未找到原生 input，点击包含文本的标签或自定义 div
    if not checked:
        try:
            label_els = driver.find_elements(By.XPATH, "//*[contains(text(), 'I confirm I am at least 13')]")
            if label_els:
                physical_click(driver, label_els[0])
                print("  ✅ 已点击条款文字标签触发勾选")
                checked = True
        except Exception as e:
            print(f"  ⚠️ 勾选异常: {e}")

    time.sleep(1.5)

    # 5. 点击 Sign in 提交按钮
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

    # 6. 等待页面跳转并处理可能弹出的验证码
    print("⏳ 等待跳转控制台...", flush=True)
    for sec in range(15):
        time.sleep(2)
        url = driver.current_url.lower()
        if "login" not in url:
            print(f"  🎉 登录成功！当前页面: {driver.current_url}", flush=True)
            return True

        # 如果跳转中出现验证码弹窗，再次破盾
        try:
            driver.uc_gui_click_captcha()
        except Exception:
            pass

    print(f"  ❌ 登录超时，停留在: {driver.current_url}")
    return False


def get_server_status(driver):
    body = driver.get_text("body")
    remaining_text = "未知"
    state_match = re.search(r"Next renewal\s*([0-9a-zA-Z\s]+?remaining)", body, re.IGNORECASE)
    if state_match:
        remaining_text = state_match.group(1).strip()

    server_state = "active"
    if "suspended" in body.lower():
        server_state = "suspended (已挂起)"
    elif "active" in body.lower():
        server_state = "active (运行中)"

    return server_state, remaining_text


def main():
    print("=== Vektal Nodes 账号密码续期测试启动 ===", flush=True)

    if not VEKTAL_EMAIL or not VEKTAL_PASSWORD:
        print("❌ 环境变量中未检测到 VEKTAL_EMAIL 或 VEKTAL_PASSWORD！")
        return

    # 启动浏览器
    driver = Driver(uc=True, headless=False, chromium_arg="--window-size=1400,1000")

    try:
        # 1. 执行账号密码登录
        if not login_with_credentials(driver, VEKTAL_EMAIL, VEKTAL_PASSWORD):
            driver.save_screenshot("login_failed.png")
            tg_send("🔴 <b>Vektal Nodes 账号密码登录失败</b>\n\n请查看排查截图。", photo_path="login_failed.png")
            return

        # 2. 进入主控制台
        driver.get(DASHBOARD_URL)
        time.sleep(5)

        # 3. 前往 Renew Server 页面
        print("📄 正在前往 Renew Server 页面...", flush=True)
        renew_links = driver.find_elements(By.XPATH, "//*[contains(text(), 'Renew Server') or contains(text(), 'Renew')]")
        if renew_links and renew_links[0].is_displayed():
            physical_click(driver, renew_links[0])
            time.sleep(4)
        else:
            driver.get(RENEW_PAGE_URL)
            time.sleep(4)

        # 4. 获取服务器状态与周期
        server_state, remaining = get_server_status(driver)
        print(f"📊 当前状态: {server_state} | 周期剩余: {remaining}")

        # 5. 查找续期/恢复按钮
        renew_btn_xpaths = [
            "//button[contains(., 'Check renewal now') or contains(., 'Renew') or contains(., 'Restore')]",
            "//*[contains(@class, 'button') or self::a][contains(., 'Renew') or contains(., 'Restore')]",
            "//button[contains(text(), '0 Coins')]"
        ]

        renew_executed = False
        for xpath in renew_btn_xpaths:
            btns = driver.find_elements(By.XPATH, xpath)
            for b in btns:
                if b.is_displayed() and b.is_enabled():
                    print(f"🎯 命中操作按钮: [{b.text.strip()}]，执行点击...")
                    physical_click(driver, b)
                    time.sleep(4)
                    renew_executed = True
                    break
            if renew_executed:
                break

        driver.save_screenshot("vektal_result.png")
        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        _, new_remaining = get_server_status(driver)

        result_msg = "✅ 成功点击续期/检测" if renew_executed else "ℹ️ 周期未到期，无需操作"
        tg_send(
            f"📋 <b>Vektal Nodes 账密测试报告</b>\n\n"
            f"🔑 <b>登录结果：</b><code>成功</code>\n"
            f"🖥️ <b>服务器状态：</b><code>{server_state}</code>\n"
            f"⏳ <b>周期剩余：</b><code>{new_remaining}</code>\n"
            f"📊 <b>续期动作：</b><code>{result_msg}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>",
            photo_path="vektal_result.png"
        )
        print("✅ 测试执行完毕！")

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}")
        driver.save_screenshot("error.png")
        tg_send(f"🔴 <b>Vektal 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="error.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
