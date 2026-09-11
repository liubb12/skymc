def click_renew(sb):
    print("🔍 查找 Renew 按钮...")
    # 精确匹配图片中的 Renew 按钮（带有时钟图标或文本）
    selectors = [
        'button:contains("Renew")',
        'button:has(svg.fa-clock)',
        'button[aria-label*="renew" i]'
    ]
    
    clicked = False
    for sel in selectors:
        try:
            if sb.is_element_visible(sel):
                sb.uc_click(sel)
                print(f"✅ 已成功点击 Renew 按钮（{sel}）")
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        # JS 强力匹配
        try:
            clicked = sb.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var i = 0; i < btns.length; i++) {
                    var b = btns[i];
                    if (b.innerText.indexOf('Renew') !== -1) {
                        b.click();
                        return true;
                    }
                }
                return false;
            """)
            if clicked:
                print("✅ 已通过 JS 强力点击 Renew 按钮")
        except Exception as e:
            print(f"❌ 点击 Renew 异常: {e}")

    if not clicked:
        print("⚠️ 未找到 Renew 按钮（请确认假人当前是否已在服中，只有在服时才会出现）")
        safe_screenshot(sb, "renew_not_found.png")
        return False

    time.sleep(3)
    if challenge_visible(sb):
        handle_cloudflare(sb, max_retry=3)
        time.sleep(2)
        
    return True
