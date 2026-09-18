#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# SkyMC 自动续期脚本 (适配最新左下角弹出式 Renew 交互)
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

BASE_URL = "https://skymc.io"
LOGIN_URL = f"{BASE_URL}/login"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

SKYMC_EMAIL = os.environ.get("SKYMC_EMAIL", "").strip() or os.environ.get("SKYMC_USERNAME", "").strip()
SKYMC_PASSWORD = os.environ.get("SKYMC_PASSWORD", "").strip()


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


def capture_screenshot_smart(driver, save_path="skymc_result.png"):
    """使用精准视口或物理屏幕抓取截屏"""
    try:
        driver.save_screenshot(save_path)
        if os.path.exists(save_path) and os.path.getsize(save_path) > 15000:
            return True
    except Exception:
        pass
    try:
        subprocess.run(["scrot", save_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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


def login_skymc(driver, email, password) -> bool:
    print(f"🌐 打开 SkyMC 登录页: {LOGIN_URL} ...", flush=True)
    driver.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5)
    time.sleep(4)

    # 如果已经在控制台
    if "/login" not in driver.current_url.lower():
        print("  🎉 已处于登录状态！")
        return True

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

    # 点击提交登录
    submit_btns = driver.find_elements(
        By.XPATH,
        "//button[contains(., 'Sign in') or contains(., 'Login') or contains(., 'Log In') or @type='submit']"
    )
    for b in submit_btns:
        if b.is_displayed():
            physical_click(driver, b)
            print("  ✅ 已点击登录按钮")
            break

    # 等待登录成功跳转
    print("⏳ 等待跳转控制台...", flush=True)
    for _ in range(20):
        time.sleep(2)
        url = driver.current_url.lower()
        if "login" not in url:
            print(f"  🎉 登录成功！当前页面: {driver.current_url}")
            return True
        try:
            driver.uc_gui_click_captcha()
        except Exception:
            pass

    return False


def handle_skymc_renewal(driver):
    """
    处理新版左下角弹出式续期菜单:
    1. 定位 'Expires in ...' 触发按钮并获取时间
    2. 点击展开弹出菜单
    3. 点击弹出的 'Renew' 项
    """
    remaining_text = "未知"
    renew_executed = False

    # 1. 尝试定位左下角的 Expires in 按钮
    print("🔍 寻找左下角 [Expires in ...] 续期触发按钮...", flush=True)
    expires_btn = None
    triggers = driver.find_elements(
        By.XPATH,
        "//button[contains(., 'Expires in')] | //*[contains(@class, 'sidebar') or contains(@class, 'bottom')]//button[contains(., 'Expires')]"
    )
    for btn in triggers:
        if btn.is_displayed():
            expires_btn = btn
            btn_txt = btn.text.strip()
            print(f"  🎯 发现触发按钮: [{btn_txt}]")
            m = re.search(r"Expires in\s*([0-9a-zA-Z\s]+)", btn_txt, re.IGNORECASE)
            if m:
                remaining_text = m.group(0).strip()
            else:
                remaining_text = btn_txt
            break

    # 如果按钮文本没抓到，从页面 body 做一次兜底正则
    if remaining_text == "未知":
        body_text = driver.get_text("body")
        m = re.search(r"Expires in\s*([0-9a-zA-Z\s]+)", body_text, re.IGNORECASE)
        if m:
            remaining_text = m.group(0).strip()

    if not expires_btn:
        print("  ⚠️ 未找到包含 'Expires in' 的按钮，尝试查找任意包含 Renew 的元素...")
    else:
        # 2. 点击左下角按钮唤出弹出菜单
        print("👉 点击触发按钮展开菜单...")
        physical_click(driver, expires_btn)
        time.sleep(1.5)

    # 3. 定位弹出的 Renew 菜单项
    print("👉 寻找弹出的 [Renew] 选项...", flush=True)
    renew_candidates = driver.find_elements(
        By.XPATH,
        "//button[contains(., 'Renew')] | //a[contains(., 'Renew')] | //div[contains(@role, 'menu')]//*[contains(text(), 'Renew')] | //li[contains(., 'Renew')]"
    )

    for item in renew_candidates:
        if item.is_displayed() and "renew" in item.text.strip().lower():
            print(f"🎯 命中续期按钮: [{item.text.strip()}]，正在点击...")
            physical_click(driver, item)
            time.sleep(2)
            renew_executed = True

            # 4. 检查是否有二级确认弹窗 (如 Confirm / Yes / Renew)
            confirm_btns = driver.find_elements(
                By.XPATH,
                "//div[contains(@role, 'dialog')]//button[contains(., 'Confirm') or contains(., 'Renew') or contains(., 'Yes')]"
            )
            for cb in confirm_btns:
                if cb.is_displayed():
                    print("  👉 命中确认弹窗，确认续期...")
                    physical_click(driver, cb)
                    time.sleep(2)
            break

    # 重新读取一次当前状态
    server_status = "Online (运行中)"
    try:
        body_txt = driver.get_text("body")
        if "Offline" in body_txt:
            server_status = "Offline (已关机)"
        elif "Suspended" in body_txt:
            server_status = "Suspended (已挂起)"
    except Exception:
        pass

    return server_status, remaining_text, renew_executed


def main():
    print("=== SkyMC 自动续期任务启动 ===", flush=True)

    chromium_args = [
        "--window-size=1400,900",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))

    try:
        # 1. 登录
        if not login_skymc(driver, SKYMC_EMAIL, SKYMC_PASSWORD):
            capture_screenshot_smart(driver, "skymc_login_failed.png")
            tg_send("🔴 <b>SkyMC 登录失败</b>", photo_path="skymc_login_failed.png")
            return

        time.sleep(5)

        # 2. 如果登录后不在服务器详情页，尝试进入第一台服务器
        if "/server" not in driver.current_url.lower():
            server_links = driver.find_elements(By.XPATH, "//a[contains(@href, '/server/')]")
            if server_links and server_links[0].is_displayed():
                print("👉 进入第一台服务器控制台...")
                physical_click(driver, server_links[0])
                time.sleep(5)

        # 3. 处理左下角新版弹出式 Renew
        server_status, remaining, renewed = handle_skymc_renewal(driver)
        print(f"📊 服务器状态: {server_status} | 到期倒计时: {remaining}")

        # 4. 截图与通知
        time.sleep(2)
        capture_screenshot_smart(driver, "skymc_result.png")

        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        action_msg = "✅ 已点击 Renew 成功续期" if renewed else "ℹ️ 页面巡检完成"

        tg_send(
            f"📋 <b>SkyMC 续期报告</b>\n\n"
            f"🔑 <b>登录结果：</b><code>成功</code>\n"
            f"🖥️ <b>服务器状态：</b><code>{server_status}</code>\n"
            f"⏳ <b>到期倒计时：</b><code>{remaining}</code>\n"
            f"📊 <b>执行动作：</b><code>{action_msg}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>",
            photo_path="skymc_result.png"
        )
        print("✅ SkyMC 执行完毕！")

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}")
        capture_screenshot_smart(driver, "skymc_error.png")
        tg_send(f"🔴 <b>SkyMC 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="skymc_error.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
