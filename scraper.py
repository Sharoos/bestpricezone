import json
import random
import sqlite3
import time
import requests

# --- Configuration ---
AFFILIATE_TAG = "ref=bestpricezone"

# Standard year-round DeoDap catalog collections (excluding seasonal/holiday sales)
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
        "url": "https://deodap.in/collections/customer-favorites/products.json",
        "name": "Bestsellers",
    },
    {
        "url": "https://deodap.in/collections/kitchen-dining-items/products.json",
        "name": "Kitchenware",
    },
    {
        "url": "https://deodap.in/collections/home_decor/products.json",
        "name": "Home & Living",
    },
    {
        "url": "https://deodap.in/collections/home-improvement/products.json",
        "name": "Hardware & Tools",
    },
    {
        "url": "https://deodap.in/collections/beauty-health-grocery/products.json",
        "name": "Beauty & Personal Care",
    },
    {
        "url": "https://deodap.in/collections/jewellery-accessories/products.json",
        "name": "Jewellery & Accessories",
    },
    {
        "url": "https://deodap.in/collections/stationery/products.json",
        "name": "Stationery & Office",
    },
    {
        "url": "https://deodap.in/collections/chargers-power/products.json",
        "name": "Mobile & Gadgets",
    },
    {
        "url": "https://deodap.in/collections/mega-savings/products.json",
        "name": "Under ₹99 Store",
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
            images TEXT,
            category TEXT,
            collection_name TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    return conn, cursor

# --- 2. Categorization Logic ---
def categorize(title):
    title = title.lower()
    if any(word in title for word in ["kitchen", "cookware", "dining", "bottle", "chopper", "container", "mug", "utensil", "lamp", "light", "decor", "clock", "pillow", "curtain", "cleaning", "mop", "shelf", "rack", "hook", "lunch box", "appliance", "knife", "ice cube", "jug", "grinder", "furniture", "housekeeping"]):
        return "Home & Living"
    elif any(word in title for word in ["skincare", "makeup", "hair", "comb", "massager", "trimmer", "fragrance", "perfume", "cosmetic", "soap", "lotion", "bath", "nail", "face", "hygiene", "health", "wellness", "oral", "grooming"]):
        return "Beauty, Health & Care"
    elif any(word in title for word in ["car", "bike", "motorcycle", "vehicle", "tool", "drill", "wrench", "hardware", "cable", "charger", "usb", "earbud", "speaker", "bluetooth", "phone", "mobile", "electrical", "gadget", "auto"]):
        return "Tech, Auto & Tools"
    elif any(word in title for word in ["toy", "game", "baby", "toddler", "kid", "puzzle", "doll", "remote control", "educational", "feeding", "rattle", "teether"]):
        return "Toys, Kids & Baby"
    elif any(word in title for word in ["watch", "jewellery", "jewelry", "earring", "necklace", "pendant", "bracelet", "bangle", "ring", "keychain", "handbag", "backpack", "wallet", "purse", "clutch", "footwear", "shoe", "luggage"]):
        return "Fashion, Bags & Jewellery"
    elif any(word in title for word in ["stationer", "pen", "pencil", "notebook", "diary", "marker", "highlighter", "desk organizer", "craft", "glue", "adhesive", "file", "folder", "paper", "office", "sharpener"]):
        return "Office, School & Crafts"
    elif any(word in title for word in ["garden", "planter", "pot", "plant", "hose", "sprinkler", "solar", "insect", "pest", "barbecue", "outdoor", "pet"]):
        return "Garden & Outdoor"
    else:
        return "More Everyday Items"

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
                print(f"Finished fetching {collection_name}. Status: {response.status_code}")
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

                raw_images = product.get("images", [])
                cleaned_images = []
                for img_obj in raw_images:
                    src = img_obj.get("src", "")
                    if src.startswith("//"): src = "https:" + src
                    elif src.startswith("/"): src = "https://deodap.in" + src
                    if src: cleaned_images.append(src)

                primary_img = cleaned_images[0] if cleaned_images else ""

                created_at = product.get("created_at", "")

                scraped_items.append({
                    "title": title,
                    "price": price,
                    "mrp": mrp,
                    "discount_label": discount_label,
                    "url": full_url,
                    "image_url": primary_img,
                    "images": cleaned_images,
                    "collection_name": collection_name,
                    "created_at": created_at,
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
        combined_collections = ",".join(item["collections"])
        images_json = json.dumps(item["images"])
        cursor.execute(
            """INSERT INTO products (title, price, mrp, discount_label, url, image_url, images, category, collection_name, created_at) 
                                VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                item["title"], item["price"], item["mrp"], item["discount_label"],
                item["url"], item["image_url"], images_json, categorize(item["title"]),
                combined_collections, item["created_at"],
            ),
        )
    conn.commit()

    cursor.execute("SELECT * FROM products")
    cols = [column[0] for column in cursor.description]
    results = []
    
    for row in cursor.fetchall():
        row_dict = dict(zip(cols, row))
        try:
            row_dict["images"] = json.loads(row_dict["images"]) if row_dict.get("images") else [row_dict["image_url"]]
        except Exception:
            row_dict["images"] = [row_dict["image_url"]]
        results.append(row_dict)

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(results, f, separators=(',', ':'))

    conn.close()
    print(f"Scrape Complete. Exported {len(results)} UNIQUE items to data.json.")

if __name__ == "__main__":
    main()