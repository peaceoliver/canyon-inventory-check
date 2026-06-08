import os
import time
import sqlite3
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import pandas as pd
from bs4 import BeautifulSoup
import streamlit as st

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# --- ADATBÁZIS KEZELÉS (SQLite) ---
DB_FILE = "canyon_watcher.db"

def init_db():
    """Létrehozza a keresési feltételek és a beállítások tábláját, ha még nem léteznek."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Figyelt kerékpárok táblája
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watched_bikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            size TEXT NOT NULL,
            added_at TEXT NOT NULL
        )
    ''')
    # Globális értesítési e-mail cím táblája
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

def get_watched_bikes():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT id, model, size FROM watched_bikes", conn)
    conn.close()
    return df

def add_watched_bike(model, size):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO watched_bikes (model, size, added_at) VALUES (?, ?, ?)", 
                   (model.strip().lower(), size, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def delete_watched_bike(bike_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM watched_bikes WHERE id = ?", (bike_id,))
    conn.commit()
    conn.close()

def get_email_setting():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'recipient_email'")
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "valaki@example.com"

def save_email_setting(email):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('recipient_email', ?)", (email.strip(),))
    conn.commit()
    conn.close()

# --- BÖNGÉSZŐ ÉS SCRAPER MÓDOSÍTÁSOK ---

def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    try:
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        st.error(f"Hiba a böngésző indításakor: {e}")
        return None

def scrape_canyon_all_watched(watched_df):
    """Végigmegy a listában szereplő összes kerékpáron és összegyűjti a találatokat"""
    driver = init_driver()
    if not driver or watched_df.empty:
        if driver: driver.quit()
        return []
    
    all_results = []
    
    for _, row in watched_df.iterrows():
        model = row['model']
        size = row['size']
        
        base_url = "https://www.canyon.com/en-ro/shop/"
        params = f"?prefn1=pc_rahmengroesse&prefv1={size}&q={model}&searchType=bikes&srule=sort_master_availability_in-stock-prio"
        full_url = base_url + params
        
        try:
            driver.get(full_url)
            time.sleep(4)  # Rövid szünet a kérések között, hogy a szerver ne blokkoljon
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            cards = soup.find_all('div', class_='productTileCard') or soup.find_all('li', class_='xlt-searchresultproduct')
            
            for card in cards:
                try:
                    name_el = card.find('div', class_='productTileCard__title') or card.find('a', class_='productTileCard__link')
                    name = name_el.text.strip() if name_el else "Ismeretlen modell"
                    
                    price_el = card.find('div', class_='productTileCard__priceSale') or card.find('span', class_='price__value')
                    price = price_el.text.strip() if price_el else "N/A"
                    
                    link_el = card.find('a', class_='productTileCard__link')
                    link = "https://www.canyon.com" + link_el['href'] if link_el else full_url
                    
                    all_results.append({
                        "Keresett Modell": model.upper(),
                        "Keresett Méret": size,
                        "Pontos Megnevezés": name,
                        "Ár": price,
                        "Link": link
                    })
                except:
                    continue
        except Exception as e:
            st.warning(f"Hiba történt a {model} ({size}) szkennelésekor: {e}")
            
    driver.quit()
    return all_results

def send_email(df, recipient_email):
    sender_email = st.secrets["email"]["sender"]
    sender_password = st.secrets["email"]["password"]
    smtp_server = st.secrets["email"]["smtp_server"]
    smtp_port = int(st.secrets["email"]["smtp_port"])
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"Canyon Készlet Jelentés - {len(df)} találat ({datetime.now().strftime('%H:%M')})"
    msg['From'] = sender_email
    msg['To'] = recipient_email
    
    html_table = df.to_html(index=False, render_links=True, escape=False)
    
    html_content = f"""
    <html>
      <head>
        <style>
          table {{ border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; }}
          th, td {{ border: 1px solid #dddddd; text-align: left; padding: 10px; }}
          th {{ background-color: #333333; color: white; }}
          tr:nth-child(even) {{ background-color: #f2f2f2; }}
          a {{ color: #FF4B4B; text-decoration: none; font-weight: bold; }}
        </style>
      </head>
      <body>
        <h2>Canyon Kerékpár Összefoglaló Jelentés</h2>
        <p>A rendszer az alábbi elérhető modelleket találta a megadott listád alapján:</p>
        {html_table}
        <br>
        <small>Az ellenőrzés automatikusan futott le.</small>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_content, 'html'))
    
    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"E-mail hiba: {e}")
        return False

# --- STREAMLIT KIJELZŐ ---

def main():
    st.set_page_config(page_title="Canyon Multi-Watcher", layout="wide", page_icon="🚲")
    init_db()
    
    st.title("🚲 Canyon.com Készlet Figyelő Központ")
    
    # 1. Beállítások szekció (E-mail)
    saved_email = get_email_setting()
    
    with st.sidebar:
        st.header("⚙️ Értesítési Beállítások")
        email_input = st.text_input("Ide küldje a jelentést:", value=saved_email)
        if st.button("Mentés", use_container_width=True):
            save_email_setting(email_input)
            st.success("E-mail cím elmentve!")
            st.rerun()
            
        st.divider()
        st.markdown("""
        ### 🔄 Automata Mód
        A háttérben futó GitHub Action óránként meghívja ezt az appot. Ilyenkor a rendszer végigmegy a jobb oldali listán, és ha van találat, küldi a levelet.
        """)

    # 2. Új kerékpár hozzáadása és meglévő lista megjelenítése
    col_add, col_list = st.columns([1, 2])
    
    with col_add:
        with st.container(border=True):
            st.subheader("➕ Új modell figyelése")
            new_model = st.text_input("Modell neve (pl: ultimate, grizl, spectral)", value="ultimate")
            new_size = st.selectbox("Méret", options=["3XS", "2XS", "XS", "S", "M", "L", "XL", "2XL"], index=4)
            
            if st.button("Hozzáadás a listához", type="primary", use_container_width=True):
                add_watched_bike(new_model, new_size)
                st.toast(f"{new_model.upper()} ({new_size}) hozzáadva!", icon="✅")
                st.rerun()

    watched_df = get_watched_bikes()

    with col_list:
        with st.container(border=True):
            st.subheader("📋 Jelenleg figyelt kerékpárok listája")
            if watched_df.empty:
                st.info("A lista még üres. Adj hozzá egy modellt a bal oldalon!")
            else:
                # Megjelenítünk egy táblázatot törlési opcióval
                for _, row in watched_df.iterrows():
                    c1, c2, c3 = st.columns([3, 1, 1])
                    c1.write(f"**Modell:** {row['model'].upper()}")
                    c2.write(f"**Méret:** {row['size']}")
                    if c3.button("❌ Törlés", key=f"del_{row['id']}"):
                        delete_watched_bike(row['id'])
                        st.rerun()

    st.divider()
    
    # 3. Manuális indító gomb az oldalon
    if st.button("🚀 Azonnali ellenőrzés és e-mail küldés (Manuális futtatás)", use_container_width=True):
        if watched_df.empty:
            st.warning("Nincs mit ellenőrizni, üres a lista!")
            return
            
        with st.status("Keresés folyamatban a Canyon oldalon...", expanded=True) as status:
            results = scrape_canyon_all_watched(watched_df)
            if results:
                status.update(label="Szkennelés kész!", state="complete")
                res_df = pd.DataFrame(results)
                st.dataframe(res_df, use_container_width=True)
                
                if send_email(res_df, email_input):
                    st.success(f"Összefoglaló e-mail elküldve ide: {email_input}")
            else:
                status.update(label="Kész, de nem találtam semmit készleten.", state="complete")
                st.info("A figyelt modellek közül jelenleg egyik sincs készleten.")

    # --- CRON / BACKGROUND TRIGGER AUTOMATIZÁCIÓ ---
    # Ha a GitHub Action meghívja az appot a háttérben (?run=true paraméterrel)
    if st.query_params.get("run") == "true":
        if not watched_df.empty:
            results = scrape_canyon_all_watched(watched_df)
            if results:
                res_df = pd.DataFrame(results)
                send_email(res_df, email_input)
            # Biztosítjuk, hogy a logokban látszódjon a Streamlitnél a lefutás
            st.write("Háttérben futó ütemezett ellenőrzés sikeresen lezajlott.")

if __name__ == "__main__":
    main()