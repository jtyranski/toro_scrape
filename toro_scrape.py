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
                status = 'success'
            else:
                status = 'fail'
            results.append({'Product #': product_num, 'status': status, 'url': url})

        # Write results to output file
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['Product #', 'status', 'url']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for row in results:
                writer.writerow(row)

        print(f"Results written to {output_file}")

    finally:
        driver.quit()

if __name__ == '__main__':
    main()