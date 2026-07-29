import json
import random
import sqlite3
import time
import requests

# --- Configuration ---
AFFILIATE_TAG = "ref=bestpricezone"

COLLECTIONS = [
    {
        "url": "https://deodap.in/collections/all/products.json",
        "name": "Main Catalog",
    },
    {
        "url": "https://deodap.in/collections/just-arrived/products.json",
        "name": "New Arrivals",
    },
    {
        "url": "https://deodap.in/collections/deodap-picks/products.json",
        "name": "Handpicked For You",
    },
    {
        "url": "https://deodap.in/collections/customer-favorites/products.json",
        "name": "Frequently Bought",
    },
]

# --- 1. Setup SQLite Database ---
def setup_database():
    conn = sqlite3.connect("affiliate_store.db")
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS products")

    cursor.execute("""
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
    """)
    conn.commit()
    return conn, cursor

# --- 2. Advanced Categorization Logic ---
def categorize(title):
    title = title.lower()
    if any(word in title for word in ["rakhi", "bhaiya", "bhabhi", "evil eye"]): return "Rakhi"
    elif any(word in title for word in ["watch", "necklace", "earring", "pendant", "bracelet", "bangle", "mangalsutra", "nose ring", "keychain", "jewelry"]): return "Jewelry & Accessories"
    elif any(word in title for word in ["perfume", "fragrance", "nail", "massager", "skincare", "makeup", "hair care", "trimmer", "cosmetic", "wellness"]): return "Health & Beauty"
    elif any(word in title for word in ["hook", "rack", "shelf", "tool", "tape", "lock", "stool", "ladder", "cabinet", "trolley", "hardware", "fixing", "drill", "wrench"]): return "Home Improvement & Tools"
    elif any(word in title for word in ["bottle", "mug", "drinkware", "utensil", "chopper", "cutter", "container", "dinnerware", "serveware", "lunch box", "appliance", "knife", "cookware", "ice cube", "jug", "kitchen", "grinder", "cleaver"]): return "Kitchen & Dining"
    elif any(word in title for word in ["lamp", "night light", "candle", "string light", "showpiece", "figurine", "wall art", "clock", "diffuser", "cushion", "lighter", "decor"]): return "Home Decor & Lighting"
    elif any(word in title for word in ["office", "pen", "pencil", "notebook", "marker", "highlighter", "desk organizer", "eraser", "stamp", "label", "craft", "file", "folder", "sharpener"]): return "Stationery & Office"
    elif any(word in title for word in ["charger", "power", "computer", "mobile", "usb", "cable", "earbud", "speaker", "bluetooth"]): return "Electronics & Accessories"
    elif any(word in title for word in ["bag", "travel", "luggage", "backpack", "umbrella", "rainwear", "camping"]): return "Travel & Outdoors"
    else: return "More Everyday Items"

# --- 3. JSON Scrape Logic with Dynamic Pagination ---
def scrape_products_json(collection_json_url, collection_name):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    }
    scraped_items = []
    page = 1
    limit = 250

    while True:
        paginated_url = f"{collection_json_url}?limit={limit}&page={page}"
        print(f"Fetching [{collection_name}] - JSON Page {page}...")

        try:
            response = requests.get(paginated_url, headers=headers, timeout=15)
            if response.status_code != 200:
                print(f"Failed or finished fetching {collection_name}. Status: {response.status_code}")
                break

            data = response.json()
            products = data.get("products", [])

            if not products:
                print(f"Reached end of catalog for {collection_name}.")
                break

            for product in products:
                title = product.get("title", "").strip()
                if not title: continue

                variants = product.get("variants", [])
                if variants:
                    price = float(variants[0].get("price", 0.0) or 0.0)
                    compare_price = variants[0].get("compare_at_price")
                    mrp = float(compare_price) if compare_price and float(compare_price) > price else price
                else:
                    price, mrp = 0.0, 0.0

                discount_label = f"{int(((mrp - price) / mrp) * 100)}% OFF" if mrp > price else ""

                handle = product.get("handle", "")
                base_url = f"https://deodap.in/products/{handle}"
                full_url = f"{base_url}&{AFFILIATE_TAG}" if "?" in base_url else f"{base_url}?{AFFILIATE_TAG}"

                images = product.get("images", [])
                img = images[0].get("src", "") if images else ""
                if img.startswith("//"): img = "https:" + img
                elif img.startswith("/"): img = "https://deodap.in" + img

                scraped_items.append({
                    "title": title,
                    "price": price,
                    "mrp": mrp,
                    "discount_label": discount_label,
                    "url": full_url,
                    "image_url": img,
                    "collection_name": collection_name,
                })

            page += 1
        except Exception as e:
            print(f"Error on page {page} for {collection_name}: {e}")
            break

        time.sleep(random.uniform(1, 2))

    return scraped_items

def main():
    conn, cursor = setup_database()
    scraped_products_dict = {}

    # Smart Deduplication: Keep track of multiple collections per item
    for col in COLLECTIONS:
        items = scrape_products_json(col["url"], col["name"])

        for item in items:
            url = item["url"]
            if url not in scraped_products_dict:
                scraped_products_dict[url] = item
                scraped_products_dict[url]["collections"] = [item["collection_name"]]
            else:
                if item["collection_name"] not in scraped_products_dict[url]["collections"]:
                    scraped_products_dict[url]["collections"].append(item["collection_name"])

    for url, item in scraped_products_dict.items():
        # Merge collections into a comma-separated string
        combined_collections = ",".join(item["collections"])
        cursor.execute(
            """INSERT INTO products (title, price, mrp, discount_label, url, image_url, category, collection_name) 
                                VALUES (?,?,?,?,?,?,?,?)""",
            (
                item["title"], item["price"], item["mrp"], item["discount_label"],
                item["url"], item["image_url"], categorize(item["title"]), combined_collections,
            ),
        )
    conn.commit()

    cursor.execute("SELECT * FROM products")
    cols = [column[0] for column in cursor.description]
    results = [dict(zip(cols, row)) for row in cursor.fetchall()]

    # Performance: Removed indent=4 and added compact separators to drastically reduce JSON file size
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(results, f, separators=(',', ':'))

    conn.close()
    print(f"Scrape Complete. Exported {len(results)} UNIQUE items to data.json.")

if __name__ == "__main__":
    main()