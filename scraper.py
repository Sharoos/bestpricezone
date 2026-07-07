import sqlite3
import json
import requests
import time
import random
from bs4 import BeautifulSoup

# --- Configuration ---
AFFILIATE_TAG = "ref=bestpricezone"

# Define the collections to scrape, their custom header names, and specific page limits
COLLECTIONS = [
    {"url": "https://deodap.in/collections/all", "name": "Main Catalog", "max_pages": 50},
    {"url": "https://deodap.in/collections/just-arrived", "name": "New Arrivals", "max_pages": 10},
    {"url": "https://deodap.in/collections/deodap-picks", "name": "Handpicked For You", "max_pages": 10},
    {"url": "https://deodap.in/collections/customer-favorites", "name": "Frequently Bought", "max_pages": 10}
]

# --- 1. Setup SQLite Database ---
def setup_database():
    conn = sqlite3.connect('affiliate_store.db')
    cursor = conn.cursor()
    
    # Drop table completely to force-update the schema with the new collection_name column
    cursor.execute('DROP TABLE IF EXISTS products')
    
    cursor.execute('''
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            price REAL,
            mrp REAL,
            discount_label TEXT,
            url TEXT,
            image_url TEXT,
            category TEXT,
            collection_name TEXT
        )
    ''')
    conn.commit()
    return conn, cursor

# --- 2. Categorization Logic ---
def categorize(title):
    title = title.lower()
    if any(word in title for word in ['board', 'kitchen', 'scoop', 'spice', 'knife', 'chopper', 'bottle', 'dispenser', 'container', 'tray', 'grinder', 'cleaver']):
        return "Kitchen"
    elif any(word in title for word in ['earring', 'rakhi', 'bracelet', 'jewelry', 'chain', 'stud', 'hoop', 'pendant']):
        return "Jewelry"
    elif any(word in title for word in ['robot', 'vacuum', 'electric', 'led', 'usb', 'lamp', 'fan', 'light', 'lighter', 'rechargeable', 'dryer']):
        return "Electronics"
    elif any(word in title for word in ['bag', 'travel', 'tumbler', 'luggage', 'camping', 'lantern', 'lounger', 'raincoat']):
        return "Travel & Outdoors"
    else:
        return "Home & Decor"

# --- 3. Scrape Logic ---
def scrape_products(collection_url, collection_name, max_pages):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
    scraped_items = []

    for page in range(1, max_pages + 1):
        paginated_url = f"{collection_url}?page={page}"
        print(f"Fetching [{collection_name}] - Page {page}/{max_pages}...")
        
        try:
            response = requests.get(paginated_url, headers=headers, timeout=15)
            if response.status_code != 200: 
                print(f"Reached end of {collection_name} or blocked. Status: {response.status_code}")
                break
            
            soup = BeautifulSoup(response.text, 'html.parser')
            products = soup.find_all('div', class_='card-wrapper')
            
            if not products:
                break 
                
            for product in products:
                title_tag = product.find('h3', class_='card__heading')
                if not title_tag: continue
                title = title_tag.text.strip()
                
                # Clean Price Math
                p_tag = product.find('span', class_='sale-price')
                m_tag = product.find('span', class_='compare-price')
                
                price = float(p_tag.text.replace('Rs.', '').replace(',', '').strip()) if p_tag else 0.0
                mrp = float(m_tag.text.replace('MRP', '').replace('Rs.', '').replace(',', '').strip()) if m_tag else price
                
                discount_label = f"{int(((mrp - price) / mrp) * 100)}% OFF" if mrp > price else ""
                
                # Affiliate URL
                raw_url = product.find('a', class_='full-unstyled-link')
                if not raw_url: continue
                raw_url = raw_url['href']
                
                base_url = "https://deodap.in" + (raw_url if raw_url.startswith('/') else '/' + raw_url)
                full_url = f"{base_url}&{AFFILIATE_TAG}" if '?' in base_url else f"{base_url}?{AFFILIATE_TAG}"
                
                # Image
                media_div = product.find('div', class_='card__media')
                img_tag = media_div.find('img') if media_div else None
                img = img_tag.get('src', '') if img_tag else ""
                if img.startswith('//'): img = "https:" + img
                elif img.startswith('/'): img = "https://deodap.in" + img

                scraped_items.append({
                    "title": title, "price": price, "mrp": mrp, 
                    "discount_label": discount_label, "url": full_url, 
                    "image_url": img, "collection_name": collection_name
                })
        except Exception as e: 
            print(f"Error on page {page}: {e}")
            continue
            
        time.sleep(random.uniform(2, 4))
        
    return scraped_items

def main():
    conn, cursor = setup_database()
    
    # Loop through each target collection
    for col in COLLECTIONS:
        items = scrape_products(col["url"], col["name"], col["max_pages"])
        
        for item in items:
            cursor.execute('''INSERT INTO products (title, price, mrp, discount_label, url, image_url, category, collection_name) 
                              VALUES (?,?,?,?,?,?,?,?)''', 
                           (item['title'], item['price'], item['mrp'], item['discount_label'], 
                            item['url'], item['image_url'], categorize(item['title']), item['collection_name']))
        conn.commit()
    
    # Export all compiled data to JSON
    cursor.execute('SELECT * FROM products')
    cols = [column[0] for column in cursor.description]
    results = [dict(zip(cols, row)) for row in cursor.fetchall()]
    
    with open('data.json', 'w', encoding='utf-8') as f: 
        json.dump(results, f, indent=4)
        
    conn.close()
    print(f"Scrape Complete. Exported {len(results)} total items to data.json.")

if __name__ == "__main__":
    main()