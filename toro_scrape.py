# toro_scrape.py
# Version: 0.3.1
# Last Updated: 2025-10-23

import requests
import json
import pandas as pd
import asyncio
from playwright.async_api import async_playwright
from datetime import datetime
from ftplib import FTP, error_perm
import time
import sys
import os
import logging
import argparse
import signal
import threading
import random

stop_requested = False

def signal_handler(sig, frame):
    global stop_requested
    print("\nInterrupt received, stopping gracefully...")
    stop_requested = True

signal.signal(signal.SIGINT, signal_handler)

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(BASE_DIR, "browsers")

def setup_logging_from_config(config_path):
    try:
        with open(config_path, "r") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    log_level_str = (cfg.get("log_level") or "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    logger = logging.getLogger()
    logger.setLevel(log_level)

    for h in list(logger.handlers):
        logger.removeHandler(h)

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(log_level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    log_file = cfg.get("log_file")
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            fh = logging.FileHandler(log_file, mode='w', encoding="utf-8")
            fh.setLevel(log_level)
            fh.setFormatter(formatter)
            logger.addHandler(fh)
            logger.info(f"Logging to file: {log_file}")
        except Exception as e:
            logger.warning(f"Failed to set file logging to {log_file}: {e}")

    return logger

class ToroScraperPlaywright:
    def __init__(self, config_file=None):
        if not config_file:
            config_file = os.path.join(BASE_DIR, "config.txt")
        elif not os.path.isabs(config_file):
            config_file = os.path.join(BASE_DIR, config_file)
        self.config = self.load_config(config_file)
        self.save_interval = int(self.config.get("save_interval", 0))  # 0 means no incremental saving
        self.partial_file = self.config.get("output_file", "toro_pricing_output.csv") + ".partial"
        self.processed_count = 0
        self.scraped_product_numbers = set()
        self.lock = threading.Lock()
        self.bearer_token = None
        self.session = requests.Session()
        self.results = []
        self._reauth_lock = threading.Lock()

    def _sync_reauthenticate(self) -> bool:
        with self._reauth_lock:
            try:
                def run_auth():
                    loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(loop)
                        return loop.run_until_complete(self.authenticate_with_playwright())
                    finally:
                        loop.close()

                ok = run_auth()
                if ok and self.bearer_token:
                    self.session.headers.update({"Authorization": f"Bearer {self.bearer_token}"})
                    return True
                return False
            except Exception as e:
                logging.getLogger(__name__).error(f"Re-auth wrapper failed: {e}")
                return False

    def _request_with_backoff(self, method, url, **kwargs):
        log = logging.getLogger(__name__)
        max_attempts = 4
        backoff = 1.0
        reauth_attempted = False

        for attempt in range(1, max_attempts + 1):
            try:
                resp = self.session.request(method, url, timeout=15, **kwargs)

                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    sleep_s = float(retry_after) if retry_after else backoff
                    log.warning(f"429 Rate limited. Attempt {attempt}/{max_attempts}. Sleeping {sleep_s:.1f}s")
                    time.sleep(sleep_s)
                    backoff = min(backoff * 2, 8.0)
                    continue

                if resp.status_code in (401, 403):
                    body_text = ""
                    try:
                        body_text = resp.text or ""
                    except Exception:
                        pass

                    if "Customer Product Restriction" in body_text:
                        return resp

                    if not reauth_attempted:
                        log.warning(f"{resp.status_code} for {url}. Attempting re-auth once.")
                        if self._sync_reauthenticate():
                            reauth_attempted = True
                            continue
                        else:
                            log.error("Re-auth attempt failed.")

                if 500 <= resp.status_code < 600:
                    if attempt < max_attempts:
                        log.warning(f"Server error {resp.status_code}. Attempt {attempt}/{max_attempts}. Retrying in {backoff:.1f}s")
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 8.0)
                        continue

                resp.raise_for_status()
                return resp

            except requests.RequestException as e:
                if attempt < max_attempts:
                    log.warning(f"HTTP error on {url}: {e}. Attempt {attempt}/{max_attempts}. Retrying in {backoff:.1f}s")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 8.0)
                    continue
                log.error(f"Failed after {max_attempts} attempts: {url} - {e}")
                raise

    def process_one_product_sync(self, product_number, index, total):
        log = logging.getLogger(__name__)
        global stop_requested

        if stop_requested:
            log.info(f"Skipping product {product_number} due to interrupt request")
            return None

        with self.lock:
            key = str(product_number).strip()
            if key in self.scraped_product_numbers:
                log.info(f"Skipping already-scraped product {product_number}")
                return None
            self.scraped_product_numbers.add(key)

        try:
            log.info(f"Processing product {index}/{total}: {product_number}")

            product_id = self.get_product_id_from_catalog(product_number)
            if not product_id:
                return None

            time.sleep(random.uniform(0.10, 0.40))  # jitter between calls
            result = self.get_product_pricing(product_id, product_number)
            if not result:
                return None

            time.sleep(random.uniform(0.10, 0.40))
            product_details = self.get_product_details(product_id)

            result.update({
                "short_description": product_details.get("shortDescription", ""),
                "erp_number": product_details.get("erpNumber", ""),
                "erp_description": product_details.get("erpDescription", ""),
                "large_image_url": product_details.get("largeImagePath", ""),
                "shipping_length": product_details.get("shippingLength", ""),
                "shipping_width": product_details.get("shippingWidth", ""),
                "shipping_height": product_details.get("shippingHeight", ""),
                "shipping_weight": product_details.get("shippingWeight", ""),
                "unit_of_measure": product_details.get("unitOfMeasure", ""),
                "unit_of_measure_description": product_details.get("unitOfMeasureDescription", ""),
                "availability_message": product_details.get("availability", {}).get("message", ""),
                "is_active": product_details.get("isActive", ""),
                "is_discontinued": product_details.get("isDiscontinued", ""),
                "can_back_order": product_details.get("canBackOrder", ""),
                "track_inventory": product_details.get("trackInventory", ""),
                "minimum_order_qty": product_details.get("minimumOrderQty", ""),
                "multiple_sale_qty": product_details.get("multipleSaleQty", ""),
                "sku": product_details.get("sku", ""),
                "upc_code": product_details.get("upcCode", ""),
                "model_number": product_details.get("modelNumber", ""),
                "brand": product_details.get("brand", ""),
                "product_line": product_details.get("productLine", ""),
                "tax_code1": product_details.get("taxCode1", ""),
                "tax_code2": product_details.get("taxCode2", ""),
                "tax_category": product_details.get("taxCategory", ""),
                "product_detail_url": product_details.get("productDetailUrl", ""),
                "is_special_order": product_details.get("isSpecialOrder", ""),
                "is_gift_card": product_details.get("isGiftCard", ""),
                "is_subscription": product_details.get("isSubscription", ""),
                "can_add_to_cart": product_details.get("canAddToCart", ""),
                "can_add_to_wishlist": product_details.get("canAddToWishlist", ""),
                "can_show_price": product_details.get("canShowPrice", ""),
                "can_show_unit_of_measure": product_details.get("canShowUnitOfMeasure", ""),
                "can_enter_quantity": product_details.get("canEnterQuantity", ""),
                "requires_real_time_inventory": product_details.get("requiresRealTimeInventory", ""),
                "availability_message_type": product_details.get("availability", {}).get("messageType", ""),
                "meta_description": product_details.get("metaDescription", ""),
                "meta_keywords": product_details.get("metaKeywords", ""),
                "page_title": product_details.get("pageTitle", ""),
            })

            with self.lock:
                self.results.append(result)
                self.processed_count += 1
                if self.save_interval > 0 and self.processed_count % self.save_interval == 0:
                    self.save_partial_results()

            return result

        except Exception as e:
            log.error(f"Error processing product {product_number}: {e}")
            return None

    def save_partial_results(self):
        try:
            if self.results:
                seen = set()
                deduped = []
                for r in self.results:
                    k = str(r.get("product_number", "")).strip()
                    if not k or k in seen:
                        continue
                    seen.add(k)
                    deduped.append(r)
                self.results = deduped

            df = pd.DataFrame(self.results)
            df.to_csv(self.partial_file, index=False)
            logging.getLogger(__name__).info(f"Partial results saved to {self.partial_file} ({len(self.results)} records)")
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to save partial results: {e}")

    def load_config(self, config_file):
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            logging.getLogger(__name__).info(f"Configuration loaded from {config_file}")
            return config
        except Exception as e:
            logging.getLogger(__name__).error(f"Error loading config: {e}")
            raise

    async def authenticate_with_playwright(self):
        log = logging.getLogger(__name__)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.config.get("headless_mode", True))
            context = await browser.new_context()
            page = await context.new_page()

            try:
                log.info("Starting authentication process...")

                await page.goto(self.config["login_url"])
                await page.wait_for_load_state('networkidle')

                await page.fill('#username', self.config["username"])
                await page.fill('#password', self.config["password"])
                await page.click('#signOnButton')
                await page.wait_for_load_state('networkidle')

                await page.wait_for_url("**/shop.thetorocompany.com/**", timeout=30000)

                bearer_token = await page.evaluate("""
                    () => {
                        const accessToken = localStorage.getItem('AccessToken');
                        if (accessToken) return accessToken;
                        const cookies = document.cookie.split(';');
                        for (let cookie of cookies) {
                            const [name, value] = cookie.trim().split('=');
                            if (name === 'AccessToken') return value;
                        }
                        return null;
                    }
                """)

                if not bearer_token:
                    log.info("Token not found in storage, intercepting network requests...")
                    tokens = []

                    def handle_request(request):
                        auth_header = request.headers.get('authorization', '')
                        if auth_header and auth_header.startswith('Bearer '):
                            tokens.append(auth_header.replace('Bearer ', ''))

                    page.on('request', handle_request)

                    await page.goto("https://shop.thetorocompany.com/Product_UrlRoot/41-6820")
                    await page.wait_for_timeout(7000)

                    if tokens:
                        bearer_token = tokens[0]

                if bearer_token:
                    self.bearer_token = bearer_token
                    log.info("Bearer token extracted successfully")
                    self.session.headers.update({
                        'Authorization': f'Bearer {bearer_token}',
                        'Content-Type': 'application/json',
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })
                    return True
                else:
                    log.error("Failed to extract bearer token")
                    return False

            except Exception as e:
                log.error(f"Authentication error: {e}")
                return False
            finally:
                await browser.close()

    def get_product_id_from_catalog(self, product_number):
        log = logging.getLogger(__name__)
        try:
            catalog_url = f"https://shop.thetorocompany.com/api/v1/catalogpages?path=%2FProduct_UrlRoot%2F{product_number}"
            resp = self._request_with_backoff("GET", catalog_url)

            ctype = (resp.headers.get("Content-Type") or "").lower()
            text = resp.text or ""
            if "customer product restriction" in text.lower():
                log.info(f"Restricted product (no access): {product_number}")
                return None

            if "application/json" not in ctype:
                log.warning(f"Non-JSON catalog response for {product_number} (status {resp.status_code}); skipping.")
                return None

            data = resp.json()
            product_id = data.get("productId")
            if product_id:
                log.info(f"Found productId {product_id} for product {product_number}")
                return product_id
            log.warning(f"No productId found for product {product_number}")
            return None

        except Exception as e:
            log.error(f"Error getting productId for {product_number}: {e}")
            return None

    def get_product_details(self, product_id):
        log = logging.getLogger(__name__)
        try:
            url = (
                f"https://shop.thetorocompany.com/api/v1/products/{product_id}"
                "?addToRecentlyViewed=true"
                "&expand=documents,specifications,styledproducts,htmlcontent,attributes,crosssells,pricing,relatedproducts"
                "&includeAlternateInventory=false"
                "&includeAttributes=IncludeOnProduct"
                "&replaceProducts=false"
            )
            response = self._request_with_backoff("GET", url)
            data = response.json()
            product = data.get("product", {})
            return product
        except Exception as e:
            log.error(f"Error getting product details for {product_id}: {e}")
            return {}

    def get_product_pricing(self, product_id, product_number):
        """Get product pricing from realtime pricing API"""
        log = logging.getLogger(__name__)
        try:
            pricing_url = "https://shop.thetorocompany.com/api/v1/realtimepricing"
            payload = {
                "productPriceParameters": [
                    {
                        "productId": product_id,
                        "unitOfMeasure": "EA",
                        "qtyOrdered": self.config["rsv_qty"]
                    }
                ]
            }

            response = self._request_with_backoff("POST", pricing_url, json=payload)
            data = response.json()
            pricing_results = data.get('realTimePricingResults', [])

            if pricing_results:
                result = pricing_results[0]

                inventory_data = {}
                properties = data.get('properties', {})
                if 'realTimeInventoryResults' in properties:
                    try:
                        inventory_json = json.loads(properties['realTimeInventoryResults'])
                        if product_id in inventory_json:
                            inventory_info = inventory_json[product_id]
                            inventory_data = {
                                'qty_on_hand': inventory_info.get('QtyOnHand', 0),
                                'availability_message': inventory_info.get('InventoryAvailabilityDtos', [{}])[0].get('Availability', {}).get('Message', ''),
                                'item_status': inventory_info.get('AdditionalResults', {}).get('ItemStatus', ''),
                                'available_date': inventory_info.get('AdditionalResults', {}).get('AvailableDate', '')
                            }
                    except Exception:
                        pass

                combined_result = {
                    'product_number': product_number,
                    'product_id': product_id,
                    'material_id': result.get('additionalResults', {}).get('materialId', ''),
                    'item_status': result.get('additionalResults', {}).get('itemStatus', ''),
                    'unit_list_price': result.get('unitListPrice', 0),
                    'unit_regular_price': result.get('unitRegularPrice', 0),
                    'unit_net_price': result.get('unitNetPrice', 0),
                    'actual_price': result.get('actualPrice', 0),
                    'is_on_sale': result.get('isOnSale', False),
                    'unit_of_measure': result.get('unitOfMeasure', ''),
                    'distribution_centre': result.get('additionalResults', {}).get('distributionCentre', ''),
                    'division': result.get('additionalResults', {}).get('division', ''),
                    'category_group': result.get('additionalResults', {}).get('categoryGroup', ''),  # verify key
                    'order_group': result.get('additionalResults', {}).get('orderGroup', ''),
                    **inventory_data
                }

                log.info(f"Successfully scraped data for {product_number}")
                return combined_result
            else:
                log.warning(f"No pricing results for {product_number}")
                return None

        except Exception as e:
            log.error(f"Error getting pricing for {product_number}: {e}")
            return None

    def load_input_csv(self):
        log = logging.getLogger(__name__)
        try:
            df = pd.read_csv(self.config["input_file"])

            def extract_product_number(sku):
                if pd.isna(sku) or not isinstance(sku, str):
                    return None
                if sku.startswith('TOR~'):
                    after_tor = sku[4:]
                    if '~' in after_tor:
                        product_number = after_tor.split('~')[0]
                    else:
                        product_number = after_tor
                    return str(product_number).strip()
                return None

            df['Product Number'] = df['SKU'].apply(extract_product_number)
            df_filtered = df[df['Product Number'].notna()]
            products = df_filtered['Product Number'].tolist()

            if self.save_interval > 0 and os.path.exists(self.partial_file):
                try:
                    df_partial = pd.read_csv(self.partial_file)
                    processed_products = {str(x).strip() for x in df_partial.get('product_number', []).tolist()}
                    products = [str(p).strip() for p in products]
                    products = [p for p in products if p and p not in processed_products]
                    self.results = df_partial.to_dict(orient='records')
                    self.processed_count = len(self.results)
                    self.scraped_product_numbers = set(processed_products)
                    log.info(f"Resuming from partial file with {self.processed_count} records")
                except Exception as e:
                    log.warning(f"Failed to load partial file: {e}")

            output_file = self.config.get("output_file")
            if output_file and os.path.exists(output_file):
                try:
                    df_output = pd.read_csv(output_file)
                    scraped_products = {str(x).strip() for x in df_output.get('product_number', []).tolist()}
                    products = [p for p in products if p not in scraped_products]
                    log.info(f"Excluded {len(scraped_products)} products already in output file")
                except Exception as e:
                    log.warning(f"Failed to load output file for duplicate check: {e}")

            log.info(f"Loaded {len(products)} products from {self.config['input_file']}")
            log.info(f"Filtered from {len(df)} total rows to {len(products)} valid Toro SKUs")

            max_rows = self.config.get("max_rows", "all")
            if isinstance(max_rows, str) and max_rows.lower() == "all":
                pass
            elif max_rows:
                try:
                    max_rows_int = int(max_rows)
                    products = products[:max_rows_int]
                    log.info(f"Limited to {max_rows_int} products")
                except Exception:
                    log.warning(f"Invalid max_rows value: {max_rows}, using all products.")

            products = [str(p).strip() for p in products if isinstance(p, str) and p.strip()]
            products = list(dict.fromkeys(products))
            return products

        except Exception as e:
            log.error(f"Error loading input CSV: {e}")
            raise

    def save_results_to_csv(self):
        log = logging.getLogger(__name__)
        try:
            if not self.results:
                log.warning("No results to save")
                return None

            seen = set()
            deduped = []
            for r in self.results:
                k = str(r.get("product_number", "")).strip()
                if not k or k in seen:
                    continue
                seen.add(k)
                deduped.append(r)
            self.results = deduped

            df = pd.DataFrame(self.results)

            output_file = self.config["output_file"]
            if os.path.exists(output_file) and not self.config.get("overwrite_existing", True):
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                name, ext = os.path.splitext(output_file)
                output_file = f"{name}_{timestamp}{ext}"

            df.to_csv(output_file, index=False)
            log.info(f"Results saved to {output_file}")
            log.info(f"Total products scraped: {len(self.results)}")

            if self.save_interval > 0 and os.path.exists(self.partial_file):
                os.remove(self.partial_file)
                logging.getLogger(__name__).info(f"Deleted partial file {self.partial_file}")

            return output_file

        except Exception as e:
            log.error(f"Error saving results: {e}")
            return None

    def upload_via_ftp(self, local_path):
        host = (self.config.get("ftp_host") or "").strip()
        user = (self.config.get("ftp_username") or "").strip()
        pwd = (self.config.get("ftp_password") or "").strip()
        port = (self.config.get("ftp_port", 21))
        remote_dir = (self.config.get("ftp_directory") or "").strip()
        log = logging.getLogger(__name__)

        if not host or not user or not pwd:
            log.info("FTP settings not provided; skipping FTP upload.")
            return False

        if not os.path.isfile(local_path):
            log.error(f"FTP upload skipped: file does not exist: {local_path}")
            return False

        try:
            log.info(f"Connecting to FTP {host}:{port} ...")
            with FTP() as ftp:
                ftp.connect(host, port, timeout=30)
                ftp.login(user, pwd)
                log.info("FTP login successful.")

                if remote_dir:
                    try:
                        ftp.cwd(remote_dir)
                    except error_perm:
                        log.info(f"FTP directory '{remote_dir}' not found; attempting to create it.")
                        for part in remote_dir.replace("\\", "/").split("/"):
                            if not part:
                                continue
                            try:
                                ftp.mkd(part)
                            except error_perm:
                                pass
                            ftp.cwd(part)

                filename = os.path.basename(local_path)
                with open(local_path, "rb") as f:
                    log.info(f"Uploading {filename} ...")
                    ftp.storbinary(f"STOR {filename}", f)

                log.info("FTP upload completed successfully.")
                return True

        except Exception as e:
            log.error(f"FTP upload failed: {e}")
            return False

    async def scrape_all_products(self):
        log = logging.getLogger(__name__)
        global stop_requested
        try:
            if not await self.authenticate_with_playwright():
                log.error("Authentication failed")
                return False

            products = self.load_input_csv()
            total = len(products)

            concurrency = int(self.config.get("concurrency", 6))
            log.info(f"Starting threaded scrape with concurrency={concurrency}")

            from concurrent.futures import ThreadPoolExecutor, as_completed

            results_local = []
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {}
                for i, pn in enumerate(products):
                    if stop_requested:
                        log.info("Interrupt detected, stopping submission of new products")
                        break
                    futures[executor.submit(self.process_one_product_sync, pn, i + 1, total)] = pn

                for fut in as_completed(futures):
                    try:
                        res = fut.result()
                        if res:
                            results_local.append(res)
                    except Exception as e:
                        pn = futures[fut]
                        log.error(f"Unhandled exception for product {pn}: {e}")

            self.results.extend(results_local)

            if stop_requested:
                self.save_partial_results()
                log.info("Scraping interrupted by user. Partial results saved.")
                return False

            output_path = self.save_results_to_csv()
            if output_path:
                self.upload_via_ftp(output_path)

            log.info("Scraping completed successfully")
            return True

        except Exception as e:
            log.error(f"Error in scraping workflow: {e}")
            return False

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.txt", help="Path to config JSON file")
    parser.add_argument("--concurrency", type=int, default=None, help="Max concurrent product requests")
    args = parser.parse_args()

    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(BASE_DIR, args.config)
    setup_logging_from_config(cfg_path)
    logger = logging.getLogger(__name__)

    scraper = ToroScraperPlaywright(cfg_path)
    if args.concurrency is not None:
        scraper.config["concurrency"] = args.concurrency
    success = await scraper.scrape_all_products()

    if success:
        print("✅ Scraping completed successfully!")
    else:
        print("❌ Scraping failed!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScript interrupted by user. Exiting...")
        sys.exit(0)