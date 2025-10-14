# toro_scrape.py
# Version: 0.2.0
# Last Updated: 2025-10-13

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

BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(BASE_DIR, "browsers")

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class ToroScraperPlaywright:
    def __init__(self, config_file=None):
        if not config_file:
            config_file = os.path.join(BASE_DIR, "config.txt")
        elif not os.path.isabs(config_file):
            config_file = os.path.join(BASE_DIR, config_file)
        self.config = self.load_config(config_file)
        self.bearer_token = None
        self.session = requests.Session()
        self.results = []

    def load_config(self, config_file):
        """Load configuration from JSON file"""
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            logger.info(f"Configuration loaded from {config_file}")
            return config
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise

    async def authenticate_with_playwright(self):
        """Use Playwright to authenticate and extract bearer token"""
        async with async_playwright() as p:
            # Launch browser
            browser = await p.chromium.launch(
                headless=self.config.get("headless_mode", True)
            )
            context = await browser.new_context()
            page = await context.new_page()

            try:
                logger.info("Starting authentication process...")

                # Navigate to login URL
                await page.goto(self.config["login_url"])
                await page.wait_for_load_state('networkidle')

                # Fill in credentials
                await page.fill('#username', self.config["username"])
                await page.fill('#password', self.config["password"])

                # Submit login form
                await page.click('#signOnButton')
                await page.wait_for_load_state('networkidle')

                # Wait for redirect to shop site
                await page.wait_for_url("**/shop.thetorocompany.com/**", timeout=30000)

                # Extract bearer token from local storage or cookies
                bearer_token = await page.evaluate("""
                    () => {
                        // Try to get from localStorage
                        const accessToken = localStorage.getItem('AccessToken');
                        if (accessToken) return accessToken;

                        // Try to get from cookies
                        const cookies = document.cookie.split(';');
                        for (let cookie of cookies) {
                            const [name, value] = cookie.trim().split('=');
                            if (name === 'AccessToken') return value;
                        }
                        return null;
                    }
                """)

                if not bearer_token:
                    # Try to intercept network requests to get the token
                    logger.info("Token not found in storage, intercepting network requests...")

                    # Navigate to a product page to trigger API calls
                    await page.goto("https://shop.thetorocompany.com/Product_UrlRoot/41-6820")

                    # Set up request interception
                    tokens = []

                    def handle_request(request):
                        auth_header = request.headers.get('authorization', '')
                        if auth_header.startswith('Bearer '):
                            tokens.append(auth_header.replace('Bearer ', ''))

                    page.on('request', handle_request)

                    # Wait for API calls
                    await page.wait_for_timeout(5000)

                    if tokens:
                        bearer_token = tokens[0]

                if bearer_token:
                    self.bearer_token = bearer_token
                    logger.info("Bearer token extracted successfully")

                    # Set up session headers
                    self.session.headers.update({
                        'Authorization': f'Bearer {bearer_token}',
                        'Content-Type': 'application/json',
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    })

                    return True
                else:
                    logger.error("Failed to extract bearer token")
                    return False

            except Exception as e:
                logger.error(f"Authentication error: {e}")
                return False
            finally:
                await browser.close()

    def get_product_id_from_catalog(self, product_number):
        """Get productId from catalog API"""
        try:
            catalog_url = f"https://shop.thetorocompany.com/api/v1/catalogpages?path=%2FProduct_UrlRoot%2F{product_number}"
            response = self.session.get(catalog_url)
            response.raise_for_status()

            data = response.json()
            product_id = data.get('productId')

            if product_id:
                logger.info(f"Found productId {product_id} for product {product_number}")
                return product_id
            else:
                logger.warning(f"No productId found for product {product_number}")
                return None

        except Exception as e:
            logger.error(f"Error getting productId for {product_number}: {e}")
            return None
    
    def get_product_details(self, product_id):
        try:
            url = (
                f"https://shop.thetorocompany.com/api/v1/products/{product_id}"
                "?addToRecentlyViewed=true"
                "&expand=documents,specifications,styledproducts,htmlcontent,attributes,crosssells,pricing,relatedproducts"
                "&includeAlternateInventory=false"
                "&includeAttributes=IncludeOnProduct"
                "&replaceProducts=false"
            )
            response = self.session.get(url)
            response.raise_for_status()
            data = response.json()
            product = data.get("product", {})
            return product
        except Exception as e:
            logger.error(f"Error getting product details for {product_id}: {e}")
            return {}

    def get_product_pricing(self, product_id, product_number):
        """Get product pricing from realtime pricing API"""
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

            response = self.session.post(pricing_url, json=payload)
            response.raise_for_status()

            data = response.json()
            pricing_results = data.get('realTimePricingResults', [])

            if pricing_results:
                result = pricing_results[0]

                # Parse inventory data from properties
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
                    except:
                        pass

                # Combine all data
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
                    'category_group': result.get('additionalResults', {}).get('category Group', ''),
                    'order_group': result.get('additionalResults', {}).get('orderGroup', ''),
                    **inventory_data
                }

                logger.info(f"Successfully scraped data for {product_number}")
                return combined_result
            else:
                logger.warning(f"No pricing results for {product_number}")
                return None

        except Exception as e:
            logger.error(f"Error getting pricing for {product_number}: {e}")
            return None

    def load_input_csv(self):
        """Load product numbers from input CSV"""
        try:
            df = pd.read_csv(self.config["input_file"])
            
            # Extract product numbers from SKU column
            def extract_product_number(sku):
                if pd.isna(sku) or not isinstance(sku, str):
                    return None
                
                # Look for pattern: TOR~[product_number]~ or TOR~[product_number] (end of string)
                if sku.startswith('TOR~'):
                    # Remove 'TOR~' prefix
                    after_tor = sku[4:]
                    
                    # Find the next ~ or end of string
                    if '~' in after_tor:
                        product_number = after_tor.split('~')[0]
                    else:
                        product_number = after_tor
                    
                    return product_number.strip()
            
                return None
            
            # Apply extraction to SKU column
            df['Product Number'] = df['SKU'].apply(extract_product_number)
            
            # Filter out rows where we couldn't extract a product number
            df_filtered = df[df['Product Number'].notna()]
            products = df_filtered['Product Number'].tolist()
            
            logger.info(f"Loaded {len(products)} products from {self.config['input_file']}")
            logger.info(f"Filtered from {len(df)} total rows to {len(products)} valid Toro SKUs")

            # Limit rows if specified and not "all"
            max_rows = self.config.get("max_rows", "all")
            if isinstance(max_rows, str) and max_rows.lower() == "all":
                pass
            elif max_rows:
                try:
                    max_rows_int = int(max_rows)
                    products = products[:max_rows_int]
                    logger.info(f"Limited to {max_rows_int} products")
                except Exception:
                    logger.warning(f"Invalid max_rows value: {max_rows}, using all products.")

            return products
        except Exception as e:
            logger.error(f"Error loading input CSV: {e}")
            raise

    def save_results_to_csv(self):
        """Save results to output CSV"""
        try:
            if not self.results:
                logger.warning("No results to save")
                return

            df = pd.DataFrame(self.results)

            # Check if output file exists and overwrite setting
            output_file = self.config["output_file"]
            if os.path.exists(output_file) and not self.config.get("overwrite_existing", True):
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                name, ext = os.path.splitext(output_file)
                output_file = f"{name}_{timestamp}{ext}"

            df.to_csv(output_file, index=False)
            logger.info(f"Results saved to {output_file}")
            logger.info(f"Total products scraped: {len(self.results)}")
            return output_file

        except Exception as e:
            logger.error(f"Error saving results: {e}")
            return None
    
    def upload_via_ftp(self, local_path):
        """Upload the given file to an FTP server using config values."""
        host = (self.config.get("ftp_host") or "").strip()
        user = (self.config.get("ftp_username") or "").strip()
        pwd = (self.config.get("ftp_password") or "").strip()
        port = (self.config.get("ftp_port", 21))
        remote_dir = (self.config.get("ftp_directory") or "").strip()
        
        if not host or not user or not pwd:
            logger.info("FTP settings not provided; skipping FTP upload.")
            return False
        
        if not os.path.isfile(local_path):
            logger.error(f"FTP upload skipped: file does not exist: {local_path}")
            return False
        
        try:
            logger.info(f"Connecting to FTP {host}:{port} ...")
            with FTP() as ftp:
                ftp.connect(host, port, timeout=30)
                ftp.login(user, pwd)
                logger.info("FTP login successful.")
                
                if remote_dir:
                    try:
                        ftp.cwd(remote_dir)
                    except error_perm:
                        logger.info(f"FTP directory '{remote_dir}' not found; attempting to create it.")
                        
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
                    logger.info(f"Uploading {filename} ...")
                    ftp.storbinary(f"STOR {filename}", f)
                
                logger.info("FTP upload completed successfully.")
                return True
        
        except Exception as e:
            logger.error(f"FTP upload failed: {e}")
            return False

    async def scrape_all_products(self):
        """Main scraping workflow"""
        try:
            # Authenticate
            if not await self.authenticate_with_playwright():
                logger.error("Authentication failed")
                return False

            # Load products
            products = self.load_input_csv()

            # Process each product
            for i, product_number in enumerate(products, 1):
                logger.info(f"Processing product {i}/{len(products)}: {product_number}")

                # Get product ID
                product_id = self.get_product_id_from_catalog(product_number)
                if not product_id:
                    continue
                
                # Get pricing data
                result = self.get_product_pricing(product_id, product_number)
                if not result:
                    continue
                
                # Get product details and merge
                product_details = self.get_product_details(product_id)
                # Pick the fields you want to add to your result  
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
                
                self.results.append(result)

                # Add delay to avoid rate limiting
                time.sleep(1)

            # Save results
            output_path = self.save_results_to_csv()
            
            if output_path:
                self.upload_via_ftp(output_path)

            logger.info("Scraping completed successfully")
            return True

        except Exception as e:
            logger.error(f"Error in scraping workflow: {e}")
            return False

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.txt", help="Path to config JSON file")
    args = parser.parse_args()
    
    cfg_path = args.config if os.path.isabs(args.config) else os.path.join(BASE_DIR, args.config)
    
    scraper = ToroScraperPlaywright(cfg_path)
    success = await scraper.scrape_all_products()

    if success:
        print("✅ Scraping completed successfully!")
    else:
        print("❌ Scraping failed!")

if __name__ == "__main__":
    asyncio.run(main())
