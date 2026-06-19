import os
import sys
import re
import time
import tempfile
import asyncio
from datetime import datetime
import pandas as pd
import networkx as nx
import streamlit as st
import streamlit.components.v1 as components
from pyvis.network import Network
from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

CSV_FILE = "set50_shareholders.csv"
SET50_URL = "https://www.set.or.th/th/market/index/set50/overview"
SHAREHOLDER_URL = "https://www.set.or.th/th/market/product/stock/quote/{symbol}/major-shareholders"

def parse_number(value: str) -> float:
    value = (value or "").replace(",", "").strip()
    return float(value) if value else 0.0

def normalize_shareholder_name(name: str) -> str:
    cleaned = (name or "").strip().upper()
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = cleaned.replace("PUBLIC COMPANY LIMITED", "PCL")
    cleaned = cleaned.replace("PUBLIC CO., LTD.", "PCL")
    cleaned = cleaned.replace("CO., LTD.", "CO LTD")
    return cleaned.strip(" .")

def scrape_and_save_to_csv(limit=5):
    all_records = []
    
    with sync_playwright() as p:
        executable_path = "/usr/bin/chromium" if sys.platform != "win32" else None
        
        browser = p.chromium.launch(
            executable_path=executable_path,
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu"
            ]
        )
        page = browser.new_page()
        
        # ก. ดึงรายชื่อหุ้น
        page.goto(SET50_URL, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(5000)
        page.wait_for_function("() => document.querySelectorAll('table tbody tr td:first-child a').length >= 50", timeout=120000)
        
        symbols_elements = page.eval_on_selector_all(
            "table tbody tr td:first-child a",
            "nodes => nodes.map(node => node.textContent.trim())"
        )
        
        symbols = []
        for sym in symbols_elements:
            sym = sym.strip().upper()
            if sym and sym not in symbols:
                symbols.append(sym)
                
        symbols = symbols[:limit]
        
        # ข. ดึงผู้ถือหุ้น
        progress_text = st.empty()
        progress_bar = st.progress(0)
        
        for i, symbol in enumerate(symbols):
            progress_text.text(f"กำลังดึงข้อมูล: {symbol} ({i+1}/{len(symbols)})")
            url = SHAREHOLDER_URL.format(symbol=symbol)
            page.goto(url, wait_until="domcontentloaded", timeout=120000)
            
            try:
                # 🌟 แก้ปัญหา Skeleton Loader: สั่งให้รอจนกว่า td ช่องชื่อคน จะมีตัวอักษรจริงๆ ไม่ใช่ช่องว่าง 🌟
                page.wait_for_function(
                    """
                    () => {
                        const rows = Array.from(document.querySelectorAll('[role="tabpanel"] table tbody tr'));
                        return rows.some(row => {
                            const tds = row.querySelectorAll('td');
                            return tds.length >= 4 && tds[1].innerText.trim().length > 2;
                        });
                    }
                    """,
                    timeout=25000,
                )
                page.wait_for_timeout(1000) # เผื่อเวลาให้เบราว์เซอร์เรนเดอร์ชิ้นส่วนเล็กๆ อีกนิด
                
                rows = page.eval_on_selector_all(
                    '[role="tabpanel"] table tbody tr',
                    """
                    nodes => nodes
                        .map(row => Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim()))
                        .filter(cols => cols.length >= 4 && cols[1].length > 0)
                    """
                )
                
                count = 0
                for cols in rows:
                    shareholder_name = normalize_shareholder_name(cols[1])
                    pct = parse_number(cols[3])
                    
                    # เช็คอีกชั้นว่าดึงชื่อมาได้จริงๆ (ไม่ใช่ค่าว่าง)
                    if shareholder_name:
                        all_records.append({
                            "Symbol": symbol,
                            "Shareholder": shareholder_name,
                            "Percentage": pct
                        })
                        count += 1
                        
                    if count >= 5: 
                        break
                        
            except Exception as e:
                pass 
                
            progress_bar.progress((i + 1) / len(symbols))
            
        browser.close()
        progress_text.empty()
        progress_bar.empty()
        
    df = pd.DataFrame(all_records)
    # กันเหนียว: ลบแถวที่ไม่มีชื่อหุ้นหรือชื่อผู้ถือหุ้นออกให้หมด
    df = df.dropna(subset=['Symbol', 'Shareholder'])
    df = df[df['Shareholder'] != ""]
    
    df.to_csv(CSV_FILE, index=False, encoding="utf-8-sig")
    return df

def build_network_graph(df):
    G = nx.Graph()
    for _, row in df.iterrows():
        symbol = str(row['Symbol']).strip()
        shareholder = str(row['Shareholder']).strip()
        pct = row['Percentage']
        
        if not G.has_node(symbol):
            G.add_node(symbol, group="Stock", color="#0F766E", size=30, title=f"หุ้น: {symbol}")
        if not G.has_node(shareholder):
            G.add_node(shareholder, group="Shareholder", color="#C2410C", size=15, title=f"ผู้ถือหุ้น: {shareholder}")
            
        G.add_edge(shareholder, symbol, value=pct, title=f"ถือหุ้น {pct}%")

    net = Network(height="700px", width="100%", bgcolor="#ffffff", font_color="#1F2937", notebook=False)
    net.from_nx(G)
    net.set_options("""
    var options = {
      "physics": {
        "barnesHut": {
          "gravitationalConstant": -4000,
          "centralGravity": 0.01,
          "springLength": 200,
          "springConstant": 0.02,
          "damping": 0.3,
          "avoidOverlap": 1
        },
        "minVelocity": 0.1
      }
    }
    """)
    return net

def get_file_modified_time(filepath):
    if os.path.exists(filepath):
        timestamp = os.path.getmtime(filepath)
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    return "ไม่มีไฟล์"

st.set_page_config(page_title="SET50 Shareholder Network", layout="wide")

with st.sidebar:
    st.header("⚙️ จัดการข้อมูล")
    
    last_updated = get_file_modified_time(CSV_FILE)
    if last_updated != "ไม่มีไฟล์":
        st.success(f"อัปเดตล่าสุด:\n{last_updated}")
    else:
        st.warning("ยังไม่มีฐานข้อมูล")

    scrape_limit = st.number_input("จำนวนหุ้นที่ต้องการดึง (เทสต์ 5, จริง 50)", min_value=1, max_value=50, value=5)
    
    if st.button("🔄 บังคับดึงข้อมูลใหม่ทันที", type="primary"):
        with st.spinner("กำลังเปิดเบราว์เซอร์และเขียนทับไฟล์ CSV..."):
            scrape_and_save_to_csv(limit=scrape_limit)
        st.success("อัปเดตและบันทึกทับไฟล์เดิมสำเร็จ!")
        time.sleep(1)
        st.rerun()

st.title("🕸️ SET50 Shareholder Social Network")

if os.path.exists(CSV_FILE):
    df = pd.read_csv(CSV_FILE)
    
    if not df.empty:
        col1, col2 = st.columns(2)
        col1.metric("จำนวนรายการความสัมพันธ์", len(df))
        col2.metric("จำนวนหุ้นในระบบ", df['Symbol'].nunique())
        
        with st.spinner("กำลังเรนเดอร์กราฟ..."):
            net = build_network_graph(df)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".html") as tmp_file:
                net.save_graph(tmp_file.name)
                with open(tmp_file.name, 'r', encoding='utf-8') as f:
                    source_code = f.read()
                components.html(source_code, height=750)
                
        with st.expander("📊 ดูข้อมูลดิบจากไฟล์ CSV"):
            st.dataframe(df, use_container_width=True)
    else:
        st.error("❌ ไฟล์ CSV มีอยู่แต่ไม่มีข้อมูล (ถูกข้ามจากการดึง) กรุณากดบังคับดึงข้อมูลใหม่อีกครั้ง")
else:
    st.warning(f"⚠️ ยังไม่พบไฟล์ `{CSV_FILE}` ในระบบ")
    st.info("👈 กรุณากดปุ่ม **'บังคับดึงข้อมูลใหม่ทันที'** ที่เมนูด้านซ้ายเพื่อเริ่มต้นครับ")
