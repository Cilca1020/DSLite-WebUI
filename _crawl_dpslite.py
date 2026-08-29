# -*- coding: utf-8 -*-
"""临时脚本：抓取并分析 dpsklite-webui.top"""
from curl_cffi import requests
from bs4 import BeautifulSoup

URL = "https://dpsklite-webui.top/"

r = requests.get(URL, impersonate="chrome", timeout=20)
r.encoding = "utf-8"
print("STATUS:", r.status_code)
print("FINAL_URL:", r.url)
print("=" * 60)

# 保存完整 HTML
with open("dpslite_snapshot.html", "w", encoding="utf-8") as f:
    f.write(r.text)
print("已保存完整HTML: dpslite_snapshot.html 长度:", len(r.text))
print("=" * 60)

soup = BeautifulSoup(r.text, "html.parser")
print("标题:", soup.title.string if soup.title else "无")

print("\n--- 页面里引用的静态资源 ---")
for s in soup.find_all(["script", "link"]):
    src = s.get("src") or s.get("href")
    if src:
        print(" ", s.name, src)

print("\n--- 主要区块 (header/nav/main/section/footer) ---")
for tag in soup.find_all(["header", "nav", "main", "footer", "section", "h1", "h2", "h3"]):
    txt = " ".join(tag.get_text(strip=True).split())[:80]
    if txt:
        print(f"  <{tag.name} id={tag.get('id')} class={tag.get('class')}> {txt}")

print("\n--- 所有可交互控件 (button/input/select/textarea/a) ---")
for tag in soup.find_all(["button", "input", "select", "textarea", "a"]):
    kind = tag.name
    name = tag.get("name") or tag.get("id") or tag.get("placeholder")
    text = " ".join(tag.get_text(strip=True).split())[:40]
    href = tag.get("href")
    print(f"  <{kind}> name={name} text={text} href={href}")
