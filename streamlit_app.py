import os
import time
import json
import re
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
    """Létrehozza a keresési feltételek és az utolsó kiküldött állapotok tábláit."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # KIVETTEM A DROP TABLE-T, hogy ne törölje le a regisztrált bringákat minden oldalfrissítésnél!
    
    # Struktúra pmin és pmax értékekkel (csak ha nem létezik)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watched_bikes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            size TEXT NOT NULL,
            email TEXT NOT NULL,
            pmin INTEGER DEFAULT 0,
            pmax INTEGER DEFAULT 99999,
            added_at TEXT NOT NULL
        )
    ''')
    
    # ÚJ TÁBLA: Az e-mail címenként elmentett utolsó állapot hash-eléséhez
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS last_email_states (
            email TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()


def get_watched_bikes():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # KÉNYSZERÍTETT MEZŐELLENŐRZÉS: Ha a tábla létezik, de hiányzik a pmin/pmax, hozzáadjuk őket menet közben
    try:
        cursor.execute("SELECT pmin FROM watched_bikes LIMIT 1")
    except sqlite3.OperationalError:
        # Ha hibát dob, az azért van, mert nem léteznek az új oszlopok. Hozzáadjuk őket!
        try:
            cursor.execute("ALTER TABLE watched_bikes ADD COLUMN pmin INTEGER DEFAULT 0")
            cursor.execute("ALTER TABLE watched_bikes ADD COLUMN pmax INTEGER DEFAULT 99999")
            conn.commit()
        except Exception:
            pass # Ha a tábla se létezne, az init_db majd létrehozza
            
    # Most már biztonságosan lefuthat a lekérdezés
    try:
        df = pd.read_sql_query("SELECT id, model, size, email, pmin, pmax FROM watched_bikes", conn)
    except Exception:
        # Végső mentőöv: ha teljesen sérült a tábla, visszaadunk egy üres táblázatot a megfelelő oszlopokkal
        df = pd.DataFrame(columns=['id', 'model', 'size', 'email', 'pmin', 'pmax'])
        
    conn.close()
    return df

def add_watched_bike(model, size, email, pmin, pmax):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO watched_bikes (model, size, email, pmin, pmax, added_at) VALUES (?, ?, ?, ?, ?, ?)", 
                   (model.strip().lower(), size, email.strip().lower(), int(pmin), int(pmax), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def check_and_delete_bike(bike_id, email):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM watched_bikes WHERE id = ? AND email = ?", (bike_id, email.strip().lower()))
    row = cursor.fetchone()
    
    if row:
        cursor.execute("DELETE FROM watched_bikes WHERE id = ?", (bike_id,))
        success = True
    else:
        success = False
        
    conn.commit()
    conn.close()
    return success

# --- BÖNGÉSZŐ ÉS SCRAPER MÓDOSÍTÁSOK ---

def init_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    
    # Ha a Streamlit Cloudon fut, a rendszerszintű krómot használjuk
    if os.path.exists("/usr/bin/chromium-browser"):
        chrome_options.binary_location = "/usr/bin/chromium-browser"
        try:
            return webdriver.Chrome(options=chrome_options)
        except Exception as e:
            st.error(f"Hiba a felhős böngésző indításakor: {e}")
            return None
            
    # Ha helyi környezetben (pl. VS Code) fut, letöltjük a drivert automatikusan
    try:
        service = Service(ChromeDriverManager().install())
        return webdriver.Chrome(service=service, options=chrome_options)
    except Exception as e:
        st.error(f"Hiba a helyi böngésző indításakor: {e}")
        return None

def check_and_alert(watched_df):
    """Csoportosítja a kereséseket az új /search/ alapú linkstruktúrával és a pontos HTML elemzéssel"""
    driver = init_driver()
    if not driver or watched_df.empty:
        if driver: driver.quit()
        return []
    
    email_reports = {}
    all_live_results = []
    
    for _, row in watched_df.iterrows():
        model = row['model']
        size = row['size']
        target_email = row['email'].strip().lower()
        pmin = row['pmin']
        pmax = row['pmax']
        
        # --- DINAMIKUS URL ÖSSZEÁLLÍTÁS AZ ÜRES MEZŐK KEZELÉSÉRE ---
        base_url = "https://www.canyon.com/en-ro/search/"
        
        # Alap paraméterek (Átírva a pontos Canyon szűrőkre)
        url_parts = [
            f"q={model}",
            "srule=sort_price_ascending",
            "start=0",
            "sz=100",
            "prefn1=pc_webCategory",       # 1. Szűrő típusa: Webes kategória
            "prefv1=Bikes"                 # 1. Szűrő értéke: Kizárólag Kerékpárok (Így nincs alkatrész!)
        ]
        
        # Ha a méret is meg van adva, az egy ÚJABB szűrő lesz (prefn2 és prefv2)
        if size and str(size).strip() and str(size).lower() not in ['none', 'mind']:
            url_parts.append("prefn2=pc_rahmengroesse")
            url_parts.append(f"prefv2={size}")
            
        # JAVÍTVA: priceMin és priceMax a hivatalos Canyon paraméter név!
        if pmin and str(pmin).strip() and str(pmin).lower() != 'none' and float(pmin) > 0:
            url_parts.append(f"priceMin={int(pmin)}")
            
        if pmax and str(pmax).strip() and str(pmax).lower() != 'none':
            url_parts.append(f"priceMax={int(pmax)}")
            
        # Összefűzzük a paramétereket egy szép URL-lé
        full_url = base_url + "?" + "&".join(url_parts)

        
        try:
            driver.get(full_url)
            time.sleep(5) # Megvárjuk, amíg a kártyák betöltenek
            
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            
            # Megkeressük az összes új típusú kerékpár kártyát
            cards = soup.select('div.productTileDefault')
            
            bike_list_for_this_row = []
            
            for card in cards:
                try:
                    # 1. Stratégia: Kiszedjük az adatokat a beágyazott GTM adatokból (ez a legbiztosabb)
                    gtm_data_str = card.get('data-gtm-impression', '')
                    name = None
                    gross_price = None
                    
                    if gtm_data_str:
                        try:
                            gtm_data = json.loads(gtm_data_str)
                            # A beküldött HTML-ben két event is van benne, megkeressük az item listát
                            for item_event in gtm_data:
                                if 'ecommerce' in item_event and 'items' in item_event['ecommerce']:
                                    bike_item = item_event['ecommerce']['items'][0]
                                    name = bike_item.get('item_name')
                                    gross_price = bike_item.get('gross_price')
                                    break
                                elif 'ecommerce' in item_event and 'impressions' in item_event['ecommerce']:
                                    bike_item = item_event['ecommerce']['impressions'][0]
                                    name = bike_item.get('name')
                                    gross_price = bike_item.get('metric4') # metric4 a bruttó ár náluk
                                    break
                        except Exception:
                            pass
                    
                    # 2. Stratégia: Ha a JSON valamiért hibát dobna, kiszedjük a link címéből
                    link_el = card.select_one('a.js-productTileLink')
                    if not name and link_el:
                        name = link_el.get('title', '').strip()
                        
                    if not name:
                        continue
                        
                    # Szűrés a modell nevére (pl. ultimate)
                    if model.lower() not in name.lower():
                        continue
                        
                    # Ár formázása
                    if gross_price:
                        price = f"{int(gross_price):,} €".replace(',', '.')
                    else:
                        # Végső mentőöv, ha nincs meg a JSON-ben az ár, kivadjuk az aria-labelből
                        aria_label = link_el.get('aria-label', '') if link_el else ''
                        price_match = re.search(r'Price:\s*([0-9.,\s]+€)', aria_label)
                        price = price_match.group(1).strip() if price_match else "N/A"
                    
                    # Link összerakása
                    link = link_el.get('href', full_url) if link_el else full_url
                    if link.startswith('/'):
                        link = "https://www.canyon.com" + link
                        
                    bike_data = {
                        "Modell": model.upper(),
                        "Méret": size,
                        "Megnevezés": name,
                        "Ár": price,
                        "Link": link
                    }
                    
                    if bike_data not in bike_list_for_this_row:
                        bike_list_for_this_row.append(bike_data)
                        all_live_results.append(bike_data)
                        
                except Exception as card_err:
                    continue
                    
            if bike_list_for_this_row:
                if target_email not in email_reports:
                    email_reports[target_email] = []
                email_reports[target_email].extend(bike_list_for_this_row)
                
        except Exception as e:
            st.warning(f"Hiba történt a {model} ({size}) szkennelésekor: {e}")
            
    driver.quit()
    
    # --- AZ ÖSSZESÍTETT LIVE REZULTÁTUMOK RENDEZÉSE ÁR SZERINT NÖVEKVŐBE ---
    if all_live_results:
        all_live_results = sorted(
            all_live_results, 
            key=lambda x: float(x['Ár'].replace('.', '').replace('€', '').strip()) if '€' in x['Ár'] else 999999
        )
    
    # ÚJ: Kapcsolat felépítése az intelligens állapotellenőrzéshez
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # E-mailek küldése a csoportosított adatokkal
    for email, bikes in email_reports.items():
        # Az adott felhasználó listáját is sorba rendezzük az e-mail kiküldése előtt
        bikes_sorted = sorted(
            bikes, 
            key=lambda x: float(x['Ár'].replace('.', '').replace('€', '').strip()) if '€' in x['Ár'] else 999999
        )
        
        # ÚJ: Egyedi szöveges ujjlenyomat (string) készítése a találati listából
        current_state_str = json.dumps(bikes_sorted, sort_keys=True)
        
        # Megnézzük, mi volt az utolsó elmentett állapot ehhez az e-mailhez
        cursor.execute("SELECT content_hash FROM last_email_states WHERE email = ?", (email,))
        db_row = cursor.fetchone()
        
        if db_row and db_row[0] == current_state_str:
            # Ha megegyezik a mostani állapot a legutóbbival, átugorjuk a küldést
            continue
            
        # Ha változás van (vagy még nincs elmentett állapot), elküldjük a levelet
        res_df = pd.DataFrame(bikes_sorted)
        if send_email(res_df, email):
            # Csak a sikeres kiküldés után mentjük el az új hálózati állapotot
            cursor.execute("""
                INSERT INTO last_email_states (email, content_hash, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET content_hash=excluded.content_hash, updated_at=excluded.updated_at
            """, (email, current_state_str, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            
    conn.close()
    return all_live_results

def send_email(df, recipient_email):
    sender_email = st.secrets["email"]["sender"]
    sender_password = st.secrets["email"]["password"]
    smtp_server = st.secrets["email"]["smtp_server"]
    smtp_port = int(st.secrets["email"]["smtp_port"])
    
    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"Canyon Készlet Jelentés neked! - {len(df)} találat ({datetime.now().strftime('%H:%M')})"
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
        <h2>Canyon Kerékpár Jelentés a Te megadott ársávod és szűrőid alapján</h2>
        <p>A rendszer a figyelési listád alapján az alábbi egyezéseket találta:</p>
        {html_table}
        <br>
        <small>Az ellenőrzés automatikusan futott le a Streamlit Appból.</small>
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
        print(f"E-mail küldési hiba ide: {recipient_email}, hiba: {e}")
        return False

# --- STREAMLIT KIJELZŐ ---

def main():
    st.set_page_config(page_title="Canyon Multi-User Watcher", layout="wide", page_icon="🚲")
    init_db()
    
    st.title("🚲 Canyon.com Készlet Figyelő")
    st.write("Felvehetitek a saját kereséseiteket egyéni ársávokkal és e-mail címmel.")
    
    st.divider()

    # 1. Új kerékpár hozzáadása adatokkal, árszűrővel és e-mail címmel
    col_add, col_list = st.columns([1.2, 2])
    
    with col_add:
        with st.container(border=True):
            st.subheader("➕ Új figyelés hozzáadása")
            new_model = st.text_input("Modell neve (pl: ultimate, aeroad, endurance)", value="ultimate")
            new_size = st.selectbox("Méret", options=["Mind", "3XS", "2XS", "XS", "S", "M", "L", "XL", "2XL"], index=0)
            
            # ÚJ: Árszűrő beviteli mezők
            c_pmin, c_pmax = st.columns([1, 1])
            with c_pmin:
                price_min = st.number_input("Min ár (€)", value=2000, step=100)
            with c_pmax:
                price_max = st.number_input("Max ár (€)", value=5000, step=100)
                
            user_email = st.text_input("Értesítendő e-mail cím (A te címed)", value="").strip()
            
            if st.button("Hozzáadás a listához", type="primary", width="stretch"):
                if "@" not in user_email or "." not in user_email:
                    st.error("Kérlek adj meg egy érvényes e-mail címet!")
                elif price_min > price_max:
                    st.error("A minimum ár nem lehet nagyobb a maximum árnál!")
                else:
                    add_watched_bike(new_model, new_size, user_email, price_min, price_max)
                    st.toast(f"{new_model.upper()} ({new_size}) hozzáadva az ársávval!", icon="✅")
                    st.rerun()

    watched_df = get_watched_bikes()

    # 2. Meglévő lista megjelenítése (E-mail nélkül, ársávval, tiszta táblázatként)
    with col_list:
        with st.container(border=True):
            st.subheader("📋 Aktuális megosztott figyelési lista")
            if watched_df.empty:
                st.info("A lista még üres. Valaki adjon hozzá egy modellt!")
            else:
                display_df = watched_df[['id', 'model', 'size', 'pmin', 'pmax']].copy()
                display_df['model'] = display_df['model'].str.upper()
                # Formázzuk az árakat szebben a kijelzőre
                display_df['arsav'] = display_df['pmin'].astype(str) + " € - " + display_df['pmax'].astype(str) + " €"
                display_df = display_df[['id', 'model', 'size', 'arsav']]
                display_df.columns = ['ID', 'Modell', 'Méret', 'Figyelt Ársáv']
                
                st.dataframe(display_df, width="stretch", hide_index=True)
                
                # Törlési szekció hitelesítéssel
                st.divider()
                st.markdown("##### 🛠️ Figyelés eltávolítása (Hitelesítéssel)")
                
                del_options = {f"ID {row['id']}: {row['model'].upper()} ({row['size']})": row['id'] for _, row in watched_df.iterrows()}
                
                c_sel, c_mail = st.columns([1, 1])
                with c_sel:
                    selected_to_delete = st.selectbox("Válaszd ki a törölni kívánt elemet:", options=list(del_options.keys()))
                with c_mail:
                    delete_email_input = st.text_input("Megerősítéshez írd be a hozzá tartozó e-mail címet:", value="", type="password", key="del_mail").strip()
                
                if st.button("🗑️ Figyelés törlése", type="secondary", width="stretch"):
                    if not delete_email_input:
                        st.error("A törléshez kötelező megadni a regisztrált e-mail címet!")
                    else:
                        bike_id_to_del = del_options[selected_to_delete]
                        if check_and_delete_bike(bike_id_to_del, delete_email_input):
                            st.toast("Figyelés sikeresen eltávolítva!", icon="🗑️")
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error("Sikertelen törlés! Az e-mail cím nem egyezik az ehhez az ID-hoz elmentett címmel.")

    st.divider()
    
    # 3. Manuális indító gomb az oldalon
    if st.button("🚀 Azonnali ellenőrzés indítása és e-mailek kiküldése", width="stretch"):
        if watched_df.empty:
            st.warning("Nincs mit ellenőrizni, üres a lista!")
            return
            
        with st.status("Keresés folyamatban a Canyon oldalon az új szűrőkkel...", expanded=True) as status:
            live_results = check_and_alert(watched_df)
            
            if live_results:
                status.update(label="Szkennelés és hálózati szűrés kész!", state="complete")
                st.success("Találatok születtek! Az e-mail csak akkor ment ki, ha új bringa érkezett vagy változott az ár.")
                st.dataframe(pd.DataFrame(live_results), width="stretch")
            else:
                status.update(label="Kész, de a megadott ársávokban jelenleg nincs találat.", state="complete")
                st.info("A figyelt modellek közül pillanatnyilag egyik sincs készleten a megadott méretekben és ársávban.")

    # --- CRON / BACKGROUND TRIGGER AUTOMATIZÁCIÓ ---
    #cron-job.org
    if st.query_params.get("run") == "true":
        if not watched_df.empty:
            check_and_alert(watched_df)
            st.write("Háttérben futó automatikus csoportos ellenőrzés lezajlott.")

if __name__ == "__main__":
    main()