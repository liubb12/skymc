name: MCServerHost 自动开机巡检

on:
  schedule:
    - cron: '*/30 * * * *'  # 每 30 分钟执行一次 (UTC时间)
  workflow_dispatch:       # 允许手动在 Actions 页面触发测试

jobs:
  run-monitor:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout Repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install Dependencies
        run: |
          sudo apt-get update
          sudo apt-get install -y xvfb scrot x11-utils
          python -m pip install --upgrade pip
          pip install seleniumbase requests

      - name: Run MCServerHost Monitor Script
        env:
          MC_EMAIL: ${{ secrets.MC_EMAIL }}           # 在 GitHub Secrets 里配置邮箱
          MC_PASSWORD: ${{ secrets.MC_PASSWORD }}     # 在 GitHub Secrets 里配置密码
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}   # 共用之前的 TG Token
          TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}       # 共用之前的 TG Chat ID
        run: |
          # 使用虚拟显示器(xvfb)运行含界面的浏览器
          xvfb-run --server-args="-screen 0 1920x1080x24" python3 mcserver_monitor.py
