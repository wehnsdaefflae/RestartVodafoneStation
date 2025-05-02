import asyncio
import json
import os
import time # Added for the small delay
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Page,
    Browser,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError
)

# --- Configuration ---
ROUTER_IP: str = "192.168.0.1"
LOGIN_URL: str = f"http://{ROUTER_IP}/"
AUTH_FILE: str = "auth.json" # File to store credentials

# --- Selectors (Assumed correct based on provided HTML) ---
USER_FIELD_SELECTOR: str = "#UserName"
PASS_FIELD_SELECTOR: str = "#Password"
LOGIN_BUTTON_SELECTOR: str = "#LoginBtn"
LOGIN_WAIT_SELECTOR: str = "#overview_refresh" # Element expected after successful login
RESTART_SUBMENU_SELECTOR: str = "#PAGE_RESTART_RESTART" # Restart submenu item ID
RESTART_CONFIRM_SELECTOR: str = "#PAGE_RESTART_POPUP_APPLY1" # Restart confirmation button ID

# --- Script ---

async def restart_router_playwright(
    router_ip: str,
    username: Optional[str],
    password: Optional[str],
    page_ref: Optional[Page] = None # Optional Page reference for screenshots on error
) -> None:
    """
    Logs into the Vodafone Station router, navigates to the restart page,
    and initiates the restart process using Playwright in headless mode.
    """
    if not username or not password:
        print(f"ERROR: Router username or password not provided (read from {AUTH_FILE}).")
        return

    async with async_playwright() as p:
        browser: Optional[Browser] = None
        page: Optional[Page] = None # Define page here for wider scope
        try:
            print("Launching headless browser (Chromium)...")
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()
            page_ref = page # Assign to outer scope variable if needed later

            print(f"Navigating to login page: {LOGIN_URL}")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

            # Add a small fixed delay in case JS needs time to render the form elements
            print("Waiting 2 seconds after page load...")
            await page.wait_for_timeout(2000)

            # --- Login Process ---
            print("Attempting login...")
            try:
                print(f"Waiting for login field: {USER_FIELD_SELECTOR}")
                await page.wait_for_selector(USER_FIELD_SELECTOR, state="visible", timeout=20000) # Keep original timeout
                print(f"Waiting for password field: {PASS_FIELD_SELECTOR}")
                await page.wait_for_selector(PASS_FIELD_SELECTOR, state="visible", timeout=5000) # Keep original timeout

                print("Entering credentials...")
                # await page.locator(USER_FIELD_SELECTOR).fill(username)
                await page.locator(PASS_FIELD_SELECTOR).fill(password)

                print("Clicking login button...")
                await page.locator(LOGIN_BUTTON_SELECTOR).click()

            except PlaywrightTimeoutError:
                print("ERROR: Failed to find login elements or page timed out waiting for them.")
                if page: await page.screenshot(path="playwright_login_timeout_error.png") # Capture state
                return # Exit function on error
            except PlaywrightError as e:
                print(f"ERROR: Playwright error during login input/click: {e}")
                if page: await page.screenshot(path="playwright_login_element_error.png")
                return # Exit function on error

            # Wait for login to complete
            print("Waiting for login confirmation element...")
            try:
                await page.wait_for_selector(LOGIN_WAIT_SELECTOR, state="visible", timeout=20000)
                print("Login successful.")
            except PlaywrightTimeoutError:
                print("ERROR: Login failed or timed out waiting for confirmation element.")
                if page: await page.screenshot(path="playwright_login_failed_error.png")
                return # Exit function on error

            # --- Click on Restart Button ---
            print("Navigating to Neustart submenu...")
            try:
                await page.goto(f"http://{ROUTER_IP}/?status_restart&mid=StatusRestart", wait_until="domcontentloaded", timeout=2000)
                await page.locator(RESTART_SUBMENU_SELECTOR).click(timeout=10000)
            except PlaywrightTimeoutError:
                print(f"ERROR: Could not find or click the '{RESTART_SUBMENU_SELECTOR}' submenu link.")
                if page: await page.screenshot(path="playwright_restart_submenu_error.png")
                return # Exit function on error

            # --- Confirm Restart ---
            print("Looking for the restart button...")
            try:
                await page.wait_for_selector(RESTART_CONFIRM_SELECTOR, state="visible", timeout=10000)

                print("Clicking the restart button...")
                await page.locator(RESTART_CONFIRM_SELECTOR).click()
                print("Restart button clicked...")

                await page.wait_for_timeout(5000) # Allow time for dialog potentially

            except PlaywrightTimeoutError:
                print(f"ERROR: Could not find the restart button '{RESTART_CONFIRM_SELECTOR}'.")
                if page: await page.screenshot(path="playwright_restart_button_error.png")
                return # Exit function on error
            except PlaywrightError as e:
                 print(f"ERROR: Playwright error clicking restart: {e}")
                 if page: await page.screenshot(path="playwright_restart_click_error.png")
                 return # Exit function on error


            print("Router restart sequence initiated successfully (assuming dialog was handled).")
            await page.wait_for_timeout(5000) # Final wait

        except PlaywrightError as e:
            print(f"A Playwright error occurred: {e}")
            try:
                if page: await page.screenshot(path="playwright_general_error.png")
            except: pass # Ignore screenshot errors during exception handling
        except Exception as e:
            print(f"An unexpected error occurred: {e}")
            try:
                if page: await page.screenshot(path="playwright_unexpected_error.png")
            except: pass
        finally:
            if browser:
                print("Closing browser.")
                await browser.close()

if __name__ == "__main__":
    print("--- Starting Router Restart Script (Playwright) ---")

    # Ensure auth.json exists
    if not os.path.exists(AUTH_FILE):
        print(f"ERROR: Credential file '{AUTH_FILE}' not found.")
        print("Please create it with your username and password, e.g.:")
        print("""
{
  "ROUTER_USER": "your_username",
  "ROUTER_PASS": "your_password"
}
        """)
        exit(1)

    # Load credentials from auth.json
    try:
        with open(AUTH_FILE, mode="r") as f:
            auth = json.load(f)
        USERNAME: Optional[str] = auth.get("ROUTER_USER")
        PASSWORD: Optional[str] = auth.get("ROUTER_PASS")

    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error reading credentials from {AUTH_FILE}: {e}")
        USERNAME = None
        PASSWORD = None
        exit(1)

    except Exception as e:
        print(f"An unexpected error occurred reading {AUTH_FILE}: {e}")
        USERNAME = None
        PASSWORD = None
        exit(1)


    if USERNAME and PASSWORD:
        asyncio.run(restart_router_playwright(ROUTER_IP, USERNAME, PASSWORD))
    else:
         print(f"Could not load username/password from {AUTH_FILE}.")

    print("--- Router Restart Script Finished ---")