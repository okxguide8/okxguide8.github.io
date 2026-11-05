import os
import datetime
import glob
import json

# 读取外部 config.json 配置文件
config_file = "config.json"
if os.path.exists(config_file):
    with open(config_file, "r", encoding="utf-8") as f:
        exchanges = json.load(f)
else:
    # 兜底默认数据
    exchanges = {}

# 动态根据 JSON 生成交易所返佣按钮 HTML
btn_group_html = ""
for key, info in exchanges.items():
    btn_group_html += f'''
    <a href="{info["link"]}" target="_blank" style="background: #1e293b; color: #fff; padding: 12px 20px; border-radius: 8px; text-decoration: none; font-weight: bold; border: 1px solid #334155; display: inline-block; text-align: left; min-width: 200px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
        <div style="font-size: 0.95rem; color: #f8fafc; margin-bottom: 2px;">🔥 {info["name"]}</div>
        <div style="font-size: 0.75rem; color: #38bdf8; font-weight: normal;">{info["badge"]}</div>
    </a>
    '''

# 获取当前日期
today = datetime.datetime.now().strftime("%Y-%m-%d")
filename = f"news-{today}.html"

# 模拟采集到的快讯数据（包含新闻配图链接、时间、标题与描述）
news_list = [
    {
        "time": f"{today} 12:00",
        "title": "市场行情观察：主流资产震荡整理，链上活跃度持续提升",
        "desc": "今日加密市场整体保持高位震荡，头部交易所流动性充沛。分析指出，随着板块轮动加快，建议投资者注意防范杠杆风险。",
        "img": "https://images.unsplash.com/photo-1639762681485-074b7f938ba0?w=600&auto=format&fit=crop&q=80"
    },
    {
        "time": f"{today} 08:30",
        "title": "多维生态动态：Layer2 网络锁仓量稳步增长",
        "desc": "近期多条主流公链生态应用迎来更新，跨链桥与去中心化交易量表现亮眼。",
        "img": "https://images.unsplash.com/photo-1642543492481-44e81e3914a7?w=600&auto=format&fit=crop&q=80"
    }
]

# 动态生成图文快讯卡片的 HTML
news_cards_html = ""
for item in news_list:
    news_cards_html += f"""
    <div class="news-box">
        <img src="{item['img']}" alt="新闻配图" class="news-img">
        <div class="news-content">
            <div class="news-time">{item['time']}</div>
            <div class="news-title">{item['title']}</div>
            <div class="news-desc">{item['desc']}</div>
        </div>
    </div>
    """

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
        
        /* 图文卡片样式 */
        .news-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); display: flex; gap: 15px; align-items: center; }}
        .news-img {{ width: 140px; height: 95px; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: #e2e8f0; }}
        .news-content {{ flex-grow: 1; }}
        .news-time {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
        .news-title {{ font-size: 1.05rem; font-weight: bold; color: #0f172a; margin-bottom: 6px; }}
        .news-desc {{ font-size: 0.9rem; color: #475569; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
        
        /* 交易所返佣转化横幅样式 */
        .affiliate-box {{ background: linear-gradient(135deg, #1e293b, #0f172a); color: #fff; padding: 25px; border-radius: 12px; margin-top: 30px; text-align: center; }}
        .affiliate-box h3 {{ margin-top: 0; margin-bottom: 10px; font-size: 1.2rem; }}
        .affiliate-box p {{ color: #94a3b8; font-size: 0.95rem; margin-bottom: 20px; }}
        .btn-group {{ display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; }}
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

    {news_cards_html}

    <!-- 交易所返佣转化横幅 -->
    <div class="affiliate-box">
        <h3>🚀 抓住 Web3 投资先机，从合规交易所开始</h3>
        <p>立即注册全球顶尖加密货币交易所，解锁专属手续费折扣与新手福利。</p>
        <div class="btn-group">
            {btn_group_html}
        </div>
    </div>

    <footer>
        <p style="text-align: center; color: #94a3b8; font-size: 0.85rem; margin-top: 40px;">© 2026 Web3 Exchange Guide. 自动化快讯聚合系统运行中。</p>
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
    date_str = file.replace("news-", "").replace(".html", "")
    list_items_html += f"""
    <div class="news-box">
        <div class="news-content">
            <div class="news-time">{date_str} 归档</div>
            <div class="news-title"><a href="./{file}" style="text-decoration: none; color: #2563eb;">{date_str} 币圈实时快讯与行业动态</a></div>
            <div class="news-desc">点击进入阅读 {date_str} 全网加密货币市场核心动态、行情观察与项目进展完整记录。</div>
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
        .news-box {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.02); }}
        .news-time {{ font-size: 12px; color: #64748b; margin-bottom: 4px; }}
        .news-title {{ font-size: 1.05rem; font-weight: bold; color: #0f172a; margin-bottom: 6px; }}
        .news-desc {{ font-size: 0.9rem; color: #475569; }}
        .affiliate-box {{ background: linear-gradient(135deg, #1e293b, #0f172a); color: #fff; padding: 25px; border-radius: 12px; margin-top: 30px; text-align: center; }}
        .affiliate-box h3 {{ margin-top: 0; margin-bottom: 10px; font-size: 1.2rem; }}
        .affiliate-box p {{ color: #94a3b8; font-size: 0.95rem; margin-bottom: 20px; }}
        .btn-group {{ display: flex; justify-content: center; gap: 15px; flex-wrap: wrap; }}
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

    <div class="affiliate-box">
        <h3>🚀 开启 Web3 交易之旅</h3>
        <p>通过专属邀请链接注册交易所，享受最高手续费返还与新手礼包。</p>
        <div class="btn-group">
            {btn_group_html}
        </div>
    </div>

    <footer>
        <p style="text-align: center; color: #94a3b8; font-size: 0.85rem; margin-top: 40px;">© 2026 Web3 Exchange Guide. 自动化快讯聚合系统运行中。</p>
    </footer>

</body>
</html>
"""

with open("news.html", "w", encoding="utf-8") as f:
    f.write(index_html_content)

print("成功更新聚合导航页：news.html")
