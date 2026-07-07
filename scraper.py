import json
import random
import sqlite3
import time
import requests
from bs4 import BeautifulSoup  # kept for compatibility if needed elsewhere

# --- Configuration ---
AFFILIATE_TAG = "ref=bestpricezone"

# Updated to target Shopify JSON endpoints directly
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
        "url": (
            "https://deodap.in/collections/customer-favorites/products.json"
        ),
        "name": "Frequently Bought",
    },
]


# --- 1. Setup SQLite Database ---
def setup_database():
  conn = sqlite3.connect("affiliate_store.db")
  cursor = conn.cursor()

  # Drop table completely to force-update the schema with the new collection_name column
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


# --- 2. Categorization Logic ---
def categorize(title):
  title = title.lower()
  if any(
      word in title
      for word in [
          "board",
          "kitchen",
          "scoop",
          "spice",
          "knife",
          "chopper",
          "bottle",
          "dispenser",
          "container",
          "tray",
          "grinder",
          "cleaver",
      ]
  ):
    return "Kitchen"
  elif any(
      word in title
      for word in [
          "earring",
          "rakhi",
          "bracelet",
          "jewelry",
          "chain",
          "stud",
          "hoop",
          "pendant",
      ]
  ):
    return "Jewelry"
  elif any(
      word in title
      for word in [
          "robot",
          "vacuum",
          "electric",
          "led",
          "usb",
          "lamp",
          "fan",
          "light",
          "lighter",
          "rechargeable",
          "dryer",
      ]
  ):
    return "Electronics"
  elif any(
      word in title
      for word in [
          "bag",
          "travel",
          "tumbler",
          "luggage",
          "camping",
          "lantern",
          "lounger",
          "raincoat",
      ]
  ):
    return "Travel & Outdoors"
  else:
    return "Home & Decor"


# --- 3. JSON Scrape Logic with Dynamic Pagination ---
def scrape_products_json(collection_json_url, collection_name):
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/114.0.0.0 Safari/537.36"
      )
  }
  scraped_items = []
  page = 1
  limit = 250  # Max limit per request allowed by Shopify

  while True:
    paginated_url = f"{collection_json_url}?limit={limit}&page={page}"
    print(f"Fetching [{collection_name}] - JSON Page {page}...")

    try:
      response = requests.get(paginated_url, headers=headers, timeout=15)
      if response.status_code != 200:
        print(
            f"Failed or finished fetching {collection_name}. Status:"
            f" {response.status_code}"
        )
        break

      data = response.json()
      products = data.get("products", [])

      if not products:
        print(f"Reached end of catalog for {collection_name}.")
        break

      for product in products:
        title = product.get("title", "").strip()
        if not title:
          continue

        # Extract Pricing from Variants
        variants = product.get("variants", [])
        if variants:
          price = float(variants[0].get("price", 0.0) or 0.0)
          compare_price = variants[0].get("compare_at_price")
          mrp = (
              float(compare_price)
              if compare_price and float(compare_price) > price
              else price
          )
        else:
          price = 0.0
          mrp = 0.0

        discount_label = (
            f"{int(((mrp - price) / mrp) * 100)}% OFF" if mrp > price else ""
        )

        # Affiliate URL formulation
        handle = product.get("handle", "")
        base_url = f"https://deodap.in/products/{handle}"
        full_url = (
            f"{base_url}&{AFFILIATE_TAG}"
            if "?" in base_url
            else f"{base_url}?{AFFILIATE_TAG}"
        )

        # Image extraction
        images = product.get("images", [])
        img = images[0].get("src", "") if images else ""
        if img.startswith("//"):
          img = "https:" + img
        elif img.startswith("/"):
          img = "https://deodap.in" + img

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

  # Loop through each target collection using the JSON endpoint
  for col in COLLECTIONS:
    items = scrape_products_json(col["url"], col["name"])

    for item in items:
      cursor.execute(
          """INSERT INTO products (title, price, mrp, discount_label, url, image_url, category, collection_name) 
                              VALUES (?,?,?,?,?,?,?,?)""",
          (
              item["title"],
              item["price"],
              item["mrp"],
              item["discount_label"],
              item["url"],
              item["image_url"],
              categorize(item["title"]),
              item["collection_name"],
          ),
      )
    conn.commit()

  # Export all compiled data to JSON
  cursor.execute("SELECT * FROM products")
  cols = [column[0] for column in cursor.description]
  results = [dict(zip(cols, row)) for row in cursor.fetchall()]

  with open("data.json", "w", encoding="utf-8") as f:
    json.dump(results, f, indent=4)

  conn.close()
  print(f"Scrape Complete. Exported {len(results)} total items to data.json.")


if __name__ == "__main__":
  main()