# toro_scrape.py
# Version: 0.1.0
# Last Updated: 2025-07-28

import time
import csv
import os
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

def load_config(path='config.txt'):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def read_product_numbers(csv_path):
    product_numbers = []
    with open(csv_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            product_numbers.append(row['Product #'])
    return product_numbers

def is_product_page(driver):
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "tst_productDetail_shortDescription"))
        )
        return True
    except:
        return False

def login(driver, config):
    driver.get(config['login_url'])
    time.sleep(2)  # Wait for page to load

    # Enter username
    username_input = driver.find_element(By.ID, "username")
    username_input.clear()
    username_input.send_keys(config['username'])

    # Enter password
    password_input = driver.find_element(By.ID, "password")
    password_input.clear()
    password_input.send_keys(config['password'])

    # Click the sign on button
    signon_button = driver.find_element(By.ID, "signOnButton")
    signon_button.click()
    time.sleep(3)  # Wait for login to process

def scrape_product_details(driver):
    details = {
        "Product #": "",
        "Product Name": "",
        "Quantity": "",
        "Cost": "",
        "Retail": "",
        "Description": ""
    }
    try:
        # Product #
        try:
            details["Product #"] = driver.find_element(By.CSS_SELECTION, "span.item-num-sku.product-item").text.strip()
        except:
            pass
        # Product Name
        try:
            details["Product Name"] = driver.find_element(By.ID, "tst_productDetail_shortDescription").text.strip()
        except:
            pass
        # Quantity
        try: 
            if driver.find_elements(By.CSS_SELECTOR, "span.instock"):
                details["Quantity"] = "10"
            elif driver.find_elements(By.CSS_SELECTOR, "span.outstock"):
                details["Quantity"] = "0"
            else:
                details["Quantity"] = ""
        except:
            details["Quantity"] = ""
        # Cost
        try:
            details["Cost"] = driver.find_element(By.CSS_SELECTOR, "span.unit-net-price").text.strip()
        except:
            pass
        # Retail
        try:
            details["Retail"] = driver.find_element(By.CSS_SELECTOR, "span[ng-bind*='unitListPrice']").text.strip()
        except:
            pass
        # Description
        try:
            details["Description"] = driver.find_element(By.CSS_SELECTOR, "div.item.overview-content").text.strip()
        except:
            pass
    except Exception as e:
        print(f"Error scraping product details: {e}")
    return details

def main():
    config = load_config()
    output_file = config.get('output_file', 'output.csv')
    product_url_root = "https://shop.thetorocompany.com/Product_UrlRoot"
    product_csv = 'allproductssamplefile.csv'

    # Set up Selenium WebDriver (headless optional)
    chrome_options = Options()
    # Uncomment the next line to run headless (no browser window)
    # chrome_options.add_argument("--headless")
    driver = webdriver.Chrome(options=chrome_options)

    try:
        # Log in
        login(driver, config)

        # Read product numbers
        product_numbers = read_product_numbers(product_csv)
        results = []

        for product_num in product_numbers:
            url = f"{product_url_root}/{product_num}"
            driver.get(url)

            if is_product_page(driver):
                details = scrape_product_details(driver)
                if not details["Product #"]:
                    details["Product #"] = product_num
                results.append(details)
            else:
                results.append({
                    "Product #": product_num, 
                    "Product Name": "",
                    "Quantity": "",
                    "Cost": "",
                    "Retail": "",
                    "Description": ""
                })

        # Write results to output file
        fieldnames = ["Product #", "Product Name", "Quantity", "Cost", "Retail", "Description"]
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for row in results:
                writer.writerow(row)

        print(f"Results written to {output_file}")

    finally:
        driver.quit()

if __name__ == '__main__':
    main()