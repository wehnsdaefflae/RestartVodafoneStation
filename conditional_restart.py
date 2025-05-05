import asyncio
import socket
import subprocess
import re
import os
import json
import logging
import time
import errno


# --- Configuration ---
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__)) # Assumes script is in project dir
AUTH_FILE = os.path.join(PROJECT_DIR, "auth.json") # Path to auth file
LOG_FILE = os.path.join(PROJECT_DIR, "router_check_python.log") # Log file path
LOCK_FILE = "/tmp/router_restart_python.lock" # Lock file for overlapping runs
LAST_RESTART_TS_FILE = "/tmp/router_last_restart_attempt.ts" # Timestamp file for cooldown
IFACE = "enp1s0" # Network interface to check
CHECK_PORT = 22 # Port that should be open (e.g., SSH)
CONNECTION_TIMEOUT = 15 # Seconds for socket connection attempt
LOCK_FILE_MAX_AGE_MIN = 60 # Max age for stale short-term lock file
COOLDOWN_PERIOD_MIN = 180 # Cooldown in minutes (e.g., 180 = 3 hours)

# --- Import the router restart function ---
try:
    from restart_router import restart_router_playwright, ROUTER_IP

except ImportError as error:
    logging.error("ERROR: Could not import restart_router_playwright from restart_router.py.")
    logging.error("Ensure restart_router.py is in the same directory or PYTHONPATH.")
    raise error

# --- Logging Setup ---
# (Same as before - ensures logs go to file and console)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S')
console = logging.StreamHandler()
console.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
console.setFormatter(formatter)
logging.getLogger('').addHandler(console)

# --- Lock File Handling (Timestamp and Overlap) ---
def check_and_manage_locks() -> bool:
    """Checks for recent restart attempts (cooldown) and overlapping runs.
       Returns True if execution should stop, False if it can proceed.
    """
    now = time.time()

    # 1. Check Cooldown Lock
    if os.path.exists(LAST_RESTART_TS_FILE):
        try:
            last_attempt_ts = os.path.getmtime(LAST_RESTART_TS_FILE)
            elapsed_min = (now - last_attempt_ts) / 60
            if elapsed_min < COOLDOWN_PERIOD_MIN:
                logging.info(f"Last restart attempt was {elapsed_min:.1f} minutes ago "
                             f"(less than cooldown {COOLDOWN_PERIOD_MIN} min). Skipping check.")
                return True # Stop execution due to cooldown
        except OSError as e:
            logging.error(f"Error checking cooldown timestamp file {LAST_RESTART_TS_FILE}: {e}")
            # Proceed with caution if we can't read the file? Or stop? Let's stop.
            return True

    # 2. Check Overlap Lock
    if os.path.exists(LOCK_FILE):
        try:
            lock_file_age_sec = now - os.path.getmtime(LOCK_FILE)
            if lock_file_age_sec > LOCK_FILE_MAX_AGE_MIN * 60:
                logging.warning(f"Stale overlap lock file found (older than {LOCK_FILE_MAX_AGE_MIN} min), removing: {LOCK_FILE}")
                os.remove(LOCK_FILE)
            else:
                logging.info(f"Overlap lock file exists and is recent ({int(lock_file_age_sec)}s old). Exiting.")
                return True # Stop execution due to overlap lock
        except OSError as e:
            logging.error(f"Error checking/removing overlap lock file {LOCK_FILE}: {e}")
            return True # Stop if we can't handle the lock file

    return False # Okay to proceed

def create_overlap_lock():
    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
        logging.info(f"Overlap lock file created: {LOCK_FILE}")
    except OSError as e:
        logging.error(f"Failed to create overlap lock file {LOCK_FILE}: {e}")

def remove_overlap_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
            logging.info(f"Overlap lock file removed: {LOCK_FILE}")
    except OSError as e:
        logging.error(f"Failed to remove overlap lock file {LOCK_FILE}: {e}")

def record_restart_attempt():
    """Updates the timestamp file to mark a restart attempt."""
    try:
        with open(LAST_RESTART_TS_FILE, 'w') as f:
            f.write(str(time.time()))
        logging.info(f"Recorded restart attempt timestamp in: {LAST_RESTART_TS_FILE}")
    except OSError as e:
        logging.error(f"Failed to record restart attempt timestamp {LAST_RESTART_TS_FILE}: {e}")


# --- Network Functions ---
# (get_ipv6_address and check_port_connectivity functions remain the same as previous version)
def get_ipv6_address(interface: str) -> str | None:
    """Gets the primary global dynamic IPv6 address (SLAAC) for the interface."""
    try:
        # Use ip command to get addresses
        process = subprocess.run(
            ['ip', '-6', 'addr', 'show', 'scope', 'global', 'dev', interface],
            capture_output=True, text=True, check=True, timeout=5
        )
        # Regex to find the SLAAC address (contains mngtmpaddr, not /128)
        match = re.search(r'inet6 ([a-f0-9:]+/64) scope global .*?dynamic mngtmpaddr', process.stdout)
        if match:
            ip_with_prefix = match.group(1)
            ip_address = ip_with_prefix.split('/')[0]
            logging.info(f"Determined server IPv6 address for {interface}: {ip_address}")
            return ip_address
        else:
             # Fallback
             match = re.search(r'inet6 ([a-f0-9:]+/\d+) scope global.*?dynamic', process.stdout)
             if match:
                 ip_with_prefix = match.group(1)
                 ip_address = ip_with_prefix.split('/')[0]
                 logging.warning(f"Could not find SLAAC address, using fallback dynamic global IPv6: {ip_address}")
                 return ip_address
             else:
                logging.error(f"Could not find a suitable dynamic global IPv6 address for interface {interface}.")
                return None
    except FileNotFoundError:
        logging.error("ERROR: 'ip' command not found. Cannot determine IP address.")
        return None
    except subprocess.CalledProcessError as e:
        logging.error(f"ERROR: 'ip addr' command failed: {e}\nStderr: {e.stderr}")
        return None
    except subprocess.TimeoutExpired:
        logging.error("ERROR: 'ip addr' command timed out.")
        return None
    except Exception as e:
        logging.error(f"Unexpected error getting IP address: {e}")
        return None

def check_port_connectivity(ip_address: str, port: int, timeout: int) -> bool:
    """Checks TCP connectivity. Returns True if port seems blocked/filtered (timeout), False otherwise."""
    problem_detected = False
    sock = None
    try:
        logging.info(f"Checking connectivity to [{ip_address}]:{port} with {timeout}s timeout...")
        # Resolve address making sure it's IPv6 compatible for socket
        addrinfo = socket.getaddrinfo(ip_address, port, socket.AF_INET6, socket.SOCK_STREAM)
        target_addr = addrinfo[0][4] # Use the resolved address tuple

        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex(target_addr)

        if result == 0:
            logging.info(f"Port {port} appears open.")
            problem_detected = False
        elif result == errno.ECONNREFUSED:
            logging.warning(f"Port {port} actively refused connection (closed). Not triggering restart.")
            problem_detected = False
        else:
            err_msg = os.strerror(result) if isinstance(result, int) else "Unknown Error"
            logging.warning(f"Port {port} connect_ex result: {result} ({err_msg}). Assuming filtered/problem.")
            problem_detected = True
    except socket.timeout:
        logging.warning(f"Connection to port {port} timed out. Assuming filtered/problem.")
        problem_detected = True
    except socket.gaierror as e:
         logging.error(f"Address/DNS error for {ip_address}: {e}")
         problem_detected = False # Don't restart on DNS issues
    except socket.error as e:
        logging.error(f"Socket error connecting to port {port}: {e}")
        problem_detected = False
    except Exception as e:
        logging.error(f"Unexpected error during socket check: {e}")
        problem_detected = False
    finally:
        if sock:
            sock.close()
    return problem_detected

# --- Main Execution ---
def main():
    # Check locks (cooldown and overlap) before doing anything else
    if check_and_manage_locks():
        exit(0)

    server_ip = get_ipv6_address(IFACE)
    if not server_ip:
        logging.error("Exiting due to failure retrieving server IP.")
        exit(1)

    # Perform the connectivity check
    trigger_restart = check_port_connectivity(server_ip, CHECK_PORT, CONNECTION_TIMEOUT)

    if trigger_restart:
        logging.warning(f"Problem detected for port {CHECK_PORT}. Initiating router restart.")
        create_overlap_lock() # Prevent overlap during restart attempt
        record_restart_attempt() # Record *before* attempting restart for cooldown

        # Load credentials
        username: str | None = None
        password: str | None = None
        try:
            if not os.path.exists(AUTH_FILE):
                 raise FileNotFoundError(f"Auth file not found at {AUTH_FILE}")
            with open(AUTH_FILE, mode="r") as f:
                auth = json.load(f)
            username = auth.get("ROUTER_USER")
            password = auth.get("ROUTER_PASS")
            if not username or not password:
                 raise ValueError("Username or password missing in auth file")
        except Exception as e:
            logging.error(f"ERROR reading credentials from {AUTH_FILE}: {e}")
            remove_overlap_lock() # Remove overlap lock if we fail before restarting
            exit(1)

        # Run the async restart function
        try:
            logging.info("Executing asyncio.run(restart_router_playwright(...))")
            asyncio.run(restart_router_playwright(ROUTER_IP, username, password))
            logging.info("Router restart script finished execution.")
        except Exception as e:
            logging.error(f"An error occurred while running the restart script: {e}")
            # Cooldown still applies as an attempt was made
        finally:
            remove_overlap_lock() # Remove overlap lock when attempt is finished
    else:
        logging.info("No connectivity problem detected. No action taken.")

    logging.info("Conditional check finished.")


if __name__ == "__main__":
    main()
