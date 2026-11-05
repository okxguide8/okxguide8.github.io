import os
import datetime
import glob

# 获取当前日期
today = datetime.datetime.now().strftime("%Y-%m-%d")
filename = f"news-{today}.html"

# 1. 生成今日快讯 HTML 内容
html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{today} 币圈实时快讯与行业动态 - Web3 导航</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9fafb; }}
        .header-bar {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 20px; }}
        .nav-links a {{ text-decoration: none; color: #2563eb; font-weight: 500; margin-right: 15px; }}
        .news-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); display: flex; gap: 15px; align-items: flex-start; }}
        .news-icon {{ font-size: 24px; flex-shrink: 0; margin-top: 2px; }}
        .news-time {{ font-size: 12px; color: #64748b; margin-bottom: 6px; }}
        .news-title {{ font-size: 1.1rem; font-weight: bold; color: #0f172a; margin-bottom: 8px; }}
        .news-desc {{ font-size: 0.95rem; color: #475569; }}
    </style>
</head>
<body>

    <header class="header-bar">
        <nav class="nav-links">
            <a href="./index.html">🏠 首页</a>
            <a href="./article-usstock.html">📈 美股教程</a>
            <a href="./news.html">🔥 币圈快讯</a>
        </nav>
    </header>

    <h1>🔥 {today} 币圈实时快讯</h1>
    <p>精选全网加密货币行业核心动态、政策风向与项目进展，7x24h 持续更新。</p>

    <div class="news-box">
        <div class="news-icon">📊</div>
        <div>
            <div class="news-time">{today} 12:00</div>
            <div class="news-title">市场行情观察：主流资产震荡整理，链上活跃度持续提升</div>
            <div class="news-desc">今日加密市场整体保持高位震荡，头部交易所流动性充沛。分析指出，随着板块轮动加快，建议投资者注意防范杠杆风险。</div>
        </div>
    </div>

    <div class="news-box">
        <div class="news-icon">🚀</div>
        <div>
            <div class="news-time">{today} 08:30</div>
            <div class="news-title">多维生态动态：Layer2 网络锁仓量稳步增长</div>
            <div class="news-desc">近期多条主流公链生态应用迎来更新，跨链桥与去中心化交易量表现亮眼。</div>
        </div>
    </div>

    <footer>
        <p style="text-align: center; color: #94a3b8; font-size: 0.85rem; margin-top: 40px;">© 2026 Web3 Exchange Guide. 自动采集发布系统运行中。</p>
    </footer>

</body>
</html>
"""

# 写入当天的快讯页面
with open(filename, "w", encoding="utf-8") as f:
    f.write(html_content)
print(f"成功生成今日快讯页面：{filename}")

# 2. 自动扫描所有历史快讯文件，动态生成聚合主页 news.html
news_files = sorted(glob.glob("news-*.html"), reverse=True)

list_items_html = ""
for file in news_files:
    # 从文件名解析出日期 (news-2026-11-05.html -> 2026-11-05)
    date_str = file.replace("news-", "").replace(".html", "")
    list_items_html += f"""
    <div class="news-box">
        <div class="news-icon">📄</div>
        <div>
            <div class="news-time">{date_str} 归档</div>
            <div class="news-title"><a href="./{file}" style="text-decoration: none; color: #2563eb;">{date_str} 币圈实时快讯与行业动态</a></div>
            <div class="news-desc">点击查看 {date_str} 全网加密货币市场核心动态、行情观察与项目进展完整记录。</div>
        </div>
    </div>
    """

index_html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>币圈实时快讯中心 - 历史归档导航</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px; background: #f9fafb; }}
        .header-bar {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e2e8f0; padding-bottom: 15px; margin-bottom: 20px; }}
        .nav-links a {{ text-decoration: none; color: #2563eb; font-weight: 500; margin-right: 15px; }}
        .news-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); display: flex; gap: 15px; align-items: flex-start; }}
        .news-icon {{ font-size: 24px; flex-shrink: 0; margin-top: 2px; }}
        .news-time {{ font-size: 12px; color: #64748b; margin-bottom: 6px; }}
        .news-title {{ font-size: 1.1rem; font-weight: bold; color: #0f172a; margin-bottom: 8px; }}
        .news-desc {{ font-size: 0.95rem; color: #475569; }}
    </style>
</head>
<body>

    <header class="header-bar">
        <nav class="nav-links">
            <a href="./index.html">🏠 首页</a>
            <a href="./article-usstock.html">📈 美股教程</a>
            <a href="./news.html">🔥 币圈快讯</a>
        </nav>
    </header>

    <h1>🔥 币圈实时快讯中心</h1>
    <p>汇集每日加密货币市场核心动态、板块轮动与项目最新进展，支持 7x24h 自动化追踪归档。</p>

    {list_items_html}

    <footer>
        <p style="text-align: center; color: #94a3b8; font-size: 0.85rem; margin-top: 40px;">© 2026 Web3 Exchange Guide. 自动化快讯聚合系统运行中。</p>
    </footer>

</body>
</html>
"""

# 写入聚合导航页 news.html
with open("news.html", "w", encoding="utf-8") as f:
    f.write(index_html_content)

print("成功更新聚合导航页：news.html")
