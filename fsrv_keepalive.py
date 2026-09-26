#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================
# FSRV (app.fsrv.pl) Cookie 免登 + 419 会话防过期 + Minecraft 自动开机
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

BASE_URL = "https://app.fsrv.pl"
MINECRAFT_URL = "https://app.fsrv.pl/minecraft"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()
FSRV_COOKIE = os.environ.get("FSRV_COOKIE", "").strip()


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


def capture_screenshot_smart(driver, save_path="fsrv_result.png"):
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
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", element)
        time.sleep(0.5)
    except Exception:
        pass
    try:
        ActionChains(driver).move_to_element(element).pause(0.3).click().perform()
        return
    except Exception:
        pass
    try:
        element.click()
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
        pass


def inject_cookies_and_navigate(driver, raw_cookie_str: str) -> bool:
    print("🌐 正在初始化访问 FSRV 主站...", flush=True)
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

        if name.upper() == "XSRF-TOKEN":
            continue

        for dom in [".fsrv.pl", "app.fsrv.pl"]:
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

    print(f"🚀 直达 Minecraft 控制台: {MINECRAFT_URL} ...", flush=True)
    driver.get(MINECRAFT_URL)
    time.sleep(6)

    body_text = driver.get_text("body")
    if "419" in body_text or "PAGE EXPIRED" in body_text:
        print("⚠️ 捕获到 419 页面过期，执行会话对齐硬刷新...", flush=True)
        driver.refresh()
        time.sleep(6)

    if "login" in driver.current_url.lower():
        print("❌ Cookie 凭据失效，当前停留在登录页！", flush=True)
        return False

    return True


def get_server_status_and_expiry(driver):
    body = driver.get_text("body")

    expiry_text = "未知"
    m_exp = re.search(r"(?:Tw[oó]j serwer wyga[sś]nie za|您的服务器将在)\s*([^!\n\r]+?)(?:!|后过期|过期)", body, re.IGNORECASE)
    if m_exp:
        expiry_text = m_exp.group(1).strip()
    elif "4周" in body or "4 tygodnie" in body:
        expiry_text = "约4周"

    # 增加 419 的精准拦截，防止将其误判为"正常"
    if "419" in body or "PAGE EXPIRED" in body.upper():
        status = "⚠️ 页面过期 (419)"
    elif "离线" in body or "Wyłączony" in body or "Wylaczony" in body:
        status = "离线 (已关机)"
    elif "启动中" in body or "Uruchamianie" in body:
        status = "启动中 (Starting)"
    elif "在线" in body or "Włączony" in body or "Działa" in body:
        status = "在线 (运行中)"
    else:
        status = "在线/正常"

    return status, expiry_text


def check_and_start(driver):
    status_before, expiry = get_server_status_and_expiry(driver)
    print(f"📊 当前状态: {status_before} | 到期时间: {expiry}", flush=True)

    action_desc = "无需操作 (已在运行)"

    if "离线" in status_before or "关机" in status_before or "419" in status_before:
        print("⚡ 检测到服务器已关机或状态异常，正在定位【启动 / Uruchom】开机按钮...", flush=True)

        start_btns = driver.find_elements(
            By.XPATH,
            "//button[contains(., '启动') or contains(., 'Uruchom') or contains(., 'Start')] | "
            "//*[contains(@class, 'button') or @role='button'][contains(., '启动') or contains(., 'Uruchom')]"
        )

        clicked = False
        for btn in start_btns:
            try:
                txt = btn.text.strip()
                if any(bad in txt for bad in ["停止", "删除", "Stop", "Usuń"]):
                    continue
                if btn.is_displayed() and btn.is_enabled():
                    print(f"🎯 命中开机按钮: [{txt}]，执行点击开机...", flush=True)
                    robust_click(driver, btn)
                    clicked = True
                    break
            except Exception:
                continue

        if clicked:
            time.sleep(5)  
            body_check = driver.get_text("body")
            if "419" in body_check or "PAGE EXPIRED" in body_check.upper():
                print("⚠️ 点击后遇到 419 拦截，正在回退并刷新重试...", flush=True)
                driver.get(MINECRAFT_URL)
                time.sleep(5)
                retry_btns = driver.find_elements(
                    By.XPATH,
                    "//button[contains(., '启动') or contains(., 'Uruchom')] | "
                    "//*[contains(@class, 'button')][contains(., '启动') or contains(., 'Uruchom')]"
                )
                for rb in retry_btns:
                    if rb.is_displayed():
                        print(f"🎯 重试点击开机按钮...", flush=True)
                        robust_click(driver, rb)
                        break

            print("⏳ 正在等待服务器完全启动，倒计时 120 秒...", flush=True)
            for remaining in range(120, 0, -10):
                print(f"   剩余等待时间: {remaining} 秒...", flush=True)
                time.sleep(10)
            
            # 【核心修复】：不要使用 driver.refresh()，使用干净的 driver.get()，并带上防 419 逻辑
            print("🔄 重新加载页面获取最新状态...", flush=True)
            driver.get(MINECRAFT_URL)
            time.sleep(6)
            
            body_final = driver.get_text("body")
            if "419" in body_final or "PAGE EXPIRED" in body_final.upper():
                print("⚠️ 重新加载时遇到 419，执行二次恢复...", flush=True)
                driver.get(MINECRAFT_URL)
                time.sleep(5)

            status_after, _ = get_server_status_and_expiry(driver)
            action_desc = f"⚡ 已执行开机 (最终状态更新为: {status_after})"
        else:
            action_desc = "⚠️ 未定位到可用的启动按钮"

    return status_before, expiry, action_desc


def main():
    print("=== FSRV Minecraft 自动开机巡检启动 (增强防419版) ===", flush=True)

    if not FSRV_COOKIE:
        print("❌ 未配置 FSRV_COOKIE 环境变量，请在 Secrets 中添加！", flush=True)
        return

    chromium_args = [
        "--window-size=1440,900",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ]
    driver = Driver(uc=True, headless=False, chromium_arg=" ".join(chromium_args))

    try:
        if not inject_cookies_and_navigate(driver, FSRV_COOKIE):
            capture_screenshot_smart(driver, "fsrv_login_fail.png")
            tg_send("🔴 <b>FSRV Cookie 失效，需重新抓取</b>", photo_path="fsrv_login_fail.png")
            return

        status_before, expiry, action_desc = check_and_start(driver)
        print(f"📊 巡检结果: {status_before} -> {action_desc}", flush=True)

        time.sleep(3)
        capture_screenshot_smart(driver, "fsrv_result.png")

        now_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

        tg_send(
            f"📋 <b>FSRV 实例巡检报告</b>\n\n"
            f"🔑 <b>认证方式：</b><code>Google OAuth Cookie 免登</code>\n"
            f"🎮 <b>服务类型：</b><code>Minecraft</code>\n"
            f"🖥️ <b>实例电源：</b><code>{status_before}</code>\n"
            f"⚡ <b>巡检动作：</b><code>{action_desc}</code>\n"
            f"⏳ <b>到期周期：</b><code>{expiry}</code>\n"
            f"⏰ <b>执行时间：</b><code>{now_str}</code>",
            photo_path="fsrv_result.png"
        )
        print("✅ FSRV 巡检任务执行完毕！")

    except Exception as e:
        err_msg = str(e)
        print(f"❌ 运行异常: {err_msg}")
        capture_screenshot_smart(driver, "fsrv_error.png")
        tg_send(f"🔴 <b>FSRV 运行异常</b>\n\n<code>{html.escape(err_msg)}</code>", photo_path="fsrv_error.png")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
