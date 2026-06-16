"""
NECaptcha Advanced Visual Solver - Railway Optimized
Handles: Rotation matching, color matching, letter matching, slide games
"""

import os
import sys
import time
import random
import subprocess
import threading
import shutil
import re
import gc
import platform
import io
import warnings
import logging
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS
import base64
from collections import Counter
import signal

app = Flask(__name__)
CORS(app)  # Enable CORS for Railway

_generation_thread = None
_generation_running = False
_generation_stats = {"generated": 0, "total": 0, "status": "idle"}

# Railway specific environment variables
PORT = int(os.environ.get('PORT', 6000))
SERVER_HOST = os.environ.get('TOKEN_SERVER_HOST', 'localhost')
SERVER_PORT = os.environ.get('TOKEN_SERVER_PORT', '12960')
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
SERVER_SAVE_ENDPOINT = f"{SERVER_URL}/push_token"

# Chrome paths for Railway
CHROME_BIN = os.environ.get('CHROME_BIN', '/usr/bin/google-chrome')
CHROMEDRIVER_PATH = os.environ.get('CHROMEDRIVER_PATH', '/usr/bin/chromedriver')

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_ENABLE_ONEDNN_OPTS'] = '0'
os.environ['WDM_LOG_LEVEL'] = '0'
os.environ['WDM_PRINT_FIRST_LINE'] = 'False'
os.environ['PYTHONWARNINGS'] = 'ignore'

warnings.filterwarnings('ignore')
logging.getLogger('selenium').setLevel(logging.CRITICAL)
logging.getLogger('urllib3').setLevel(logging.CRITICAL)
logging.getLogger('easyocr').setLevel(logging.CRITICAL)

def install_dependencies():
    dependencies = [
        "selenium",
        "requests",
        "webdriver-manager",
        "opencv-python",
        "numpy",
        "pillow",
        "scikit-learn",
        "scipy",
        "easyocr",
    ]
    missing = []
    for dep in dependencies:
        try:
            __import__(dep.replace('-', '_'))
        except ImportError:
            missing.append(dep)

    if missing:
        subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
    import requests
except ImportError:
    install_dependencies()

try:
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    from sklearn.cluster import KMeans
    import easyocr
except ImportError:
    pass

IS_LINUX = platform.system() == 'Linux'
IS_WINDOWS = platform.system() == 'Windows'

# Configuration
CAPTCHA_KEY = "fef5c67c39074e9d845f4bf579cc07af"
MAX_THREADS = int(os.environ.get('MAX_THREADS', 2))
NUM_BROWSERS = int(os.environ.get('NUM_BROWSERS', 2))
MAX_TOKENS_PER_BROWSER = int(os.environ.get('MAX_TOKENS_PER_BROWSER', 10))
MAX_CONSECUTIVE_FAILS = int(os.environ.get('MAX_CONSECUTIVE_FAILS', 5))
SOLVE_TIMEOUT = int(os.environ.get('SOLVE_TIMEOUT', 45))
WIN_W, WIN_H = 1280, 720
HEADLESS = os.environ.get('HEADLESS', 'true').lower() == 'true'

_print_lock = threading.Lock()

def cprint(msg):
    with _print_lock:
        print(msg)

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log_info(idx, msg):
    cprint(f"[{ts()}] [B{idx+1}] › {msg}")

def log_status(idx, attempt, gen, fcount, remaining, slot):
    cprint(f"[{ts()}] [B{idx+1}] #{attempt} ↑{gen} ◉{fcount} ◎{remaining} [{slot}/{MAX_TOKENS_PER_BROWSER}]")

def log_solving(idx, elapsed, captcha_type=""):
    cprint(f"[{ts()}] [B{idx+1}] Solving {captcha_type}... {elapsed}s")

def log_success(idx, num, tokens, extra=""):
    tok_preview = tokens[:40]
    cprint(f"[{ts()}] [B{idx+1}] ✔ #{num} {tok_preview}... {extra}")

def log_fail(idx, fail, maxf):
    cprint(f"[{ts()}] [B{idx+1}] ✗ FAIL [{fail}/{maxf}]")

def log_warn(idx, msg):
    cprint(f"[{ts()}] [B{idx+1}] ⚠ {msg}")

def log_relaunch(idx, reason):
    cprint(f"[{ts()}] [B{idx+1}] ↻ RELAUNCH {reason}")

def send_tokens_to_server(tokens, use_server=False):
    if not use_server:
        return None
    try:
        payload = {"server": "necaptcha", "token": tokens}
        r = requests.post(SERVER_SAVE_ENDPOINT, json=payload, timeout=10)
        if r.status_code in [200, 201]:
            return "ok"
        return None
    except Exception as e:
        log_warn(0, f"Server send error: {e}")
        return None

_ram_tokens_count = 0
_ram_tokens_count_lock = threading.Lock()

def count_tokens():
    with _ram_tokens_count_lock:
        return _ram_tokens_count

def increment_tokens_count():
    global _ram_tokens_count
    with _ram_tokens_count_lock:
        _ram_tokens_count += 1
        return _ram_tokens_count

def cleanup_chrome_garbage():
    base = _get_temp_base()
    prefixes = ('scoped_dir', '.com.google', 'chrome_', 'Crashpad', 'uc_')
    try:
        for name in os.listdir(base):
            if any(name.startswith(p) for p in prefixes):
                path = os.path.join(base, name)
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass
    gc.collect()

def kill_chrome():
    if IS_LINUX:
        for proc in ['chrome', 'chromedriver', 'google-chrome']:
            try:
                subprocess.run(['pkill', '-f', proc], capture_output=True, timeout=5)
            except Exception:
                pass
    else:
        for proc in ['chrome.exe', 'chromedriver.exe']:
            try:
                subprocess.run(['taskkill', '/F', '/IM', proc], capture_output=True, timeout=5)
            except Exception:
                pass

def _get_temp_base():
    return os.environ.get('TEMP', os.environ.get('TMP', '/tmp'))

def create_driver(browser_index=0):
    for attempt in range(3):
        try:
            options = Options()
            
            # Basic arguments
            for arg in [
                "--no-first-run",
                "--no-service-autorun",
                "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                "--disable-popup-blocking",
                "--disable-infobars",
                "--disable-gpu",
                "--disable-default-apps",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-logging",
                "--disable-crash-reporter",
                "--disable-sync",
                "--log-level=3",
                f"--window-size={WIN_W},{WIN_H}",
            ]:
                options.add_argument(arg)
            
            # Headless mode for Railway
            if HEADLESS:
                options.add_argument("--headless=new")
                options.add_argument("--disable-software-rasterizer")
                options.add_argument("--disable-webgl")
            
            options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
            options.add_experimental_option("useAutomationExtension", False)

            temp_dir = os.path.join(_get_temp_base(), f'uc_b{browser_index}')
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            os.makedirs(temp_dir, exist_ok=True)
            options.add_argument(f"--user-data-dir={temp_dir}")

            # Set Chrome binary
            if os.path.exists(CHROME_BIN):
                options.binary_location = CHROME_BIN

            # Set ChromeDriver service
            service = Service(CHROMEDRIVER_PATH) if os.path.exists(CHROMEDRIVER_PATH) else None
            
            if service:
                driver = webdriver.Chrome(options=options, service=service)
            else:
                driver = webdriver.Chrome(options=options)
            
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(10)
            
            # Set window size
            try:
                driver.set_window_size(WIN_W, WIN_H)
            except:
                pass
            
            return driver
        except Exception as e:
            log_warn(browser_index, f"Launch failed ({attempt+1}/3): {str(e)[:100]}")
            time.sleep(2)
    return None

# ============ Advanced Visual Captcha Solver ============

class VisualCaptchaSolver:
    def __init__(self):
        self.reader = None
        try:
            self.reader = easyocr.Reader(['en'], gpu=False, verbose=False)
            log_info(0, "EasyOCR initialized")
        except Exception as e:
            log_warn(0, f"EasyOCR not available: {e}")
        
        self.letter_templates = self._generate_letter_templates()
        
    def _generate_letter_templates(self):
        templates = {}
        try:
            font = ImageFont.load_default()
            for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                img = Image.new('L', (50, 50), 0)
                draw = ImageDraw.Draw(img)
                bbox = draw.textbbox((0, 0), letter, font=font)
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                x = (50 - w) // 2
                y = (50 - h) // 2
                draw.text((x, y), letter, fill=255, font=font)
                templates[letter] = np.array(img)
            return templates
        except:
            return {}

    def capture_element_screenshot(self, driver, element):
        try:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
            time.sleep(0.2)
            
            location = element.location
            size = element.size
            screenshot = driver.get_screenshot_as_png()
            image = Image.open(io.BytesIO(screenshot))
            
            dpr = driver.execute_script("return window.devicePixelRatio || 1;")
            
            left = int(location['x'] * dpr)
            top = int(location['y'] * dpr)
            right = int((location['x'] + size['width']) * dpr)
            bottom = int((location['y'] + size['height']) * dpr)
            
            left = max(0, left)
            top = max(0, top)
            right = min(image.size[0], right)
            bottom = min(image.size[1], bottom)
            
            if right <= left or bottom <= top:
                return None
                
            cropped = image.crop((left, top, right, bottom))
            return cropped
        except Exception as e:
            return None

    def detect_rotation_angle(self, image):
        try:
            if isinstance(image, Image.Image):
                img = np.array(image)
            else:
                img = image
            
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=30, minLineLength=10, maxLineGap=5)
            
            if lines is None:
                return 0
            
            angles = []
            for line in lines:
                x1, y1, x2, y2 = line[0]
                angle = np.arctan2(y2 - y1, x2 - x1) * 180 / np.pi
                if abs(angle) < 5:
                    angles.append(0)
                elif abs(angle - 90) < 5:
                    angles.append(0)
                else:
                    angles.append(angle)
            
            if not angles:
                return 0
            
            angle_counter = Counter(angles)
            most_common = angle_counter.most_common(1)[0][0]
            return abs(most_common)
        except Exception as e:
            return 0

    def detect_letter_ocr(self, image):
        try:
            if isinstance(image, Image.Image):
                img = np.array(image)
            else:
                img = image
            
            if self.reader:
                results = self.reader.readtext(img, detail=0, paragraph=False)
                if results and len(results) > 0:
                    text = results[0].strip().upper()
                    letters = re.findall(r'[A-Z]', text)
                    if letters:
                        return letters[0]
            
            return self._match_template(image)
        except Exception as e:
            return None

    def _match_template(self, image):
        try:
            if isinstance(image, Image.Image):
                img = np.array(image)
            else:
                img = image
            
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            
            best_match = None
            best_score = 0
            
            for letter, template in self.letter_templates.items():
                template_resized = cv2.resize(template, (thresh.shape[1], thresh.shape[0]))
                result = cv2.matchTemplate(thresh, template_resized, cv2.TM_CCOEFF_NORMED)
                score = np.max(result)
                
                if score > best_score:
                    best_score = score
                    best_match = letter
            
            if best_score > 0.3:
                return best_match
            return None
        except Exception as e:
            return None

    def detect_captcha_type(self, instruction_text, driver):
        instruction_lower = instruction_text.lower()
        
        if '颜色' in instruction_lower and '一样' in instruction_lower:
            return 'color_match'
        elif '拖动' in instruction_lower or '球' in instruction_lower or '滑动' in instruction_lower:
            return 'slide_game'
        elif '正向' in instruction_lower or '反向' in instruction_lower or '旋转' in instruction_lower:
            return 'rotation_match'
        elif '大写' in instruction_lower or '小写' in instruction_lower:
            return 'letter_match'
        elif '依次' in instruction_lower:
            return 'sequence_match'
        
        try:
            if driver.find_elements(By.CSS_SELECTOR, "div[class*='slide'], div[class*='ball'], div[class*='drag']"):
                return 'slide_game'
            if driver.find_elements(By.CSS_SELECTOR, "div[class*='grid'], div[class*='option'], div[class*='item']"):
                return 'letter_match'
        except:
            pass
        
        return 'unknown'

    def find_options_elements(self, driver):
        options = []
        selectors = [
            "div[class*='option']",
            "div[class*='item']",
            "div[class*='letter']",
            "div[class*='grid'] > div",
            "div[class*='img']",
            "div[role='button']",
            "span[class*='char']",
            "div[class*='box']",
            "li[class*='item']",
            "div[class*='click']",
        ]
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    if el.is_displayed():
                        text = el.text.strip()
                        if text and len(text) <= 2:
                            options.append(el)
            except:
                continue
        
        if not options:
            try:
                elements = driver.find_elements(By.XPATH, "//*[text() and string-length(text())=1 and not(contains(@style, 'display:none'))]")
                for el in elements:
                    if el.is_displayed():
                        options.append(el)
            except:
                pass
        
        return options

    def analyze_options(self, driver, options):
        analyzed = []
        for el in options:
            try:
                text = el.text.strip()
                if not text:
                    continue
                
                screenshot = self.capture_element_screenshot(driver, el)
                rotation = 0
                detected_letter = text
                
                if screenshot:
                    rotation = self.detect_rotation_angle(screenshot)
                    ocr_result = self.detect_letter_ocr(screenshot)
                    if ocr_result:
                        detected_letter = ocr_result
                
                color = None
                try:
                    color = el.value_of_css_property('color')
                except:
                    pass
                
                location = el.location
                
                analyzed.append({
                    'element': el,
                    'text': text,
                    'detected_letter': detected_letter,
                    'rotation': rotation,
                    'color': color,
                    'position': (location.get('x', 0), location.get('y', 0)),
                    'is_rotated': rotation > 15,
                })
            except Exception as e:
                continue
        
        return analyzed

    def solve_rotation_match(self, driver, instruction_text):
        try:
            target_letter = None
            target_case = None
            target_direction = 'positive'
            
            letter_match = re.search(r'[a-zA-Z]', instruction_text)
            if letter_match:
                target_letter = letter_match.group(0)
            
            if '大写' in instruction_text:
                target_case = 'uppercase'
            elif '小写' in instruction_text:
                target_case = 'lowercase'
            
            if '正向' in instruction_text:
                target_direction = 'positive'
            elif '反向' in instruction_text:
                target_direction = 'reverse'
            
            if not target_letter:
                log_warn(0, "No target letter found in instruction")
                return False
            
            log_info(0, f"Target: {target_direction} {target_case} '{target_letter}'")
            
            options = self.find_options_elements(driver)
            if not options:
                log_warn(0, "No options found")
                return False
            
            analyzed = self.analyze_options(driver, options)
            
            candidates = []
            for item in analyzed:
                letter = item['detected_letter'].upper()
                if letter != target_letter.upper():
                    continue
                
                if target_case == 'uppercase' and not item['text'].isupper():
                    continue
                if target_case == 'lowercase' and not item['text'].islower():
                    continue
                
                if target_direction == 'positive' and not item['is_rotated']:
                    candidates.append(item)
                elif target_direction == 'reverse' and item['is_rotated']:
                    candidates.append(item)
            
            if not candidates:
                candidates = [item for item in analyzed if item['text'].upper() == target_letter.upper()]
            
            for candidate in candidates:
                try:
                    el = candidate['element']
                    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
                    time.sleep(0.3)
                    el.click()
                    time.sleep(0.5)
                    log_info(0, f"Clicked: {el.text}")
                    return True
                except:
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        time.sleep(0.5)
                        return True
                    except:
                        continue
            
            return False
        except Exception as e:
            log_warn(0, f"Rotation match error: {e}")
            return False

    def solve_letter_match(self, driver, instruction_text):
        try:
            target_letter = None
            target_case = None
            
            letter_match = re.search(r'[a-zA-Z]', instruction_text)
            if letter_match:
                target_letter = letter_match.group(0)
            
            if '大写' in instruction_text:
                target_case = 'uppercase'
            elif '小写' in instruction_text:
                target_case = 'lowercase'
            
            if not target_letter:
                return False
            
            options = self.find_options_elements(driver)
            
            for el in options:
                text = el.text.strip()
                if text.upper() != target_letter.upper():
                    continue
                if target_case == 'uppercase' and not text.isupper():
                    continue
                if target_case == 'lowercase' and not text.islower():
                    continue
                
                try:
                    el.click()
                    time.sleep(0.5)
                    return True
                except:
                    continue
            
            return False
        except Exception as e:
            return False

    def solve_color_match(self, driver, instruction_text):
        try:
            lower_match = re.search(r'小写\s*([a-z])', instruction_text)
            upper_match = re.search(r'大写\s*([A-Z])', instruction_text)
            
            if not lower_match or not upper_match:
                return False
            
            reference_char = lower_match.group(1)
            target_char = upper_match.group(1)
            
            options = self.find_options_elements(driver)
            
            ref_element = None
            for el in options:
                if el.text.strip() == reference_char:
                    ref_element = el
                    break
            
            if not ref_element:
                return False
            
            ref_color = ref_element.value_of_css_property('color')
            
            for el in options:
                text = el.text.strip()
                if text.upper() != target_char.upper():
                    continue
                
                color = el.value_of_css_property('color')
                if color == ref_color:
                    try:
                        el.click()
                        time.sleep(0.5)
                        return True
                    except:
                        continue
            
            return False
        except Exception as e:
            return False

    def solve_sequence_match(self, driver, instruction_text):
        try:
            options = driver.find_elements(By.XPATH, "//*[text() and string-length(text())=1 and text()>='1' and text()<='9']")
            if not options:
                return False
            
            sorted_options = sorted(options, key=lambda x: int(x.text.strip()))
            
            for el in sorted_options:
                try:
                    el.click()
                    time.sleep(0.3)
                except:
                    continue
            
            return True
        except Exception as e:
            return False

    def solve_slide_captcha(self, driver):
        try:
            ball_selectors = [
                "div[class*='ball']",
                "div[class*='slide']",
                "div[class*='drag']",
                "div[class*='handle']",
                "div[class*='knob']",
                "div[class*='move']",
                "div[draggable='true']",
            ]
            
            ball = None
            for selector in ball_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        ball = elements[0]
                        break
                except:
                    continue
            
            if not ball:
                return False
            
            track_selectors = [
                "div[class*='track']",
                "div[class*='container']",
                "div[class*='slider']",
                "div[class*='bar']",
                "div[class*='path']",
                "div[class*='bg']",
            ]
            
            track = None
            for selector in track_selectors:
                try:
                    elements = driver.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        track = elements[0]
                        break
                except:
                    continue
            
            if track:
                track_width = track.size['width']
                try:
                    screenshot = self.capture_element_screenshot(driver, track)
                    if screenshot:
                        img_array = np.array(screenshot)
                        if len(img_array.shape) == 3:
                            gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
                        else:
                            gray = img_array
                        edges = cv2.Canny(gray, 50, 150)
                        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        if contours:
                            max_x = 0
                            for cnt in contours:
                                x, y, w, h = cv2.boundingRect(cnt)
                                if x + w > max_x:
                                    max_x = x + w
                            track_width = max_x
                except:
                    pass
            else:
                track_width = 350
            
            drag_distance = int(track_width * random.uniform(0.85, 0.98))
            
            action = ActionChains(driver)
            action.click_and_hold(ball)
            time.sleep(random.uniform(0.1, 0.3))
            
            steps = random.randint(12, 20)
            step_distance = drag_distance / steps
            
            for i in range(steps):
                offset = int(step_distance * random.uniform(0.7, 1.3))
                y_offset = random.randint(-3, 3)
                action.move_by_offset(offset, y_offset)
                time.sleep(random.uniform(0.015, 0.05))
            
            time.sleep(random.uniform(0.1, 0.3))
            action.release()
            action.perform()
            
            time.sleep(1)
            return True
        except Exception as e:
            return False

    def solve_unknown_captcha(self, driver):
        try:
            clickable_selectors = [
                "div[class*='click']",
                "div[class*='option']",
                "div[class*='item']",
                "div[class*='img']",
                "div[role='button']",
            ]
            
            elements = []
            for selector in clickable_selectors:
                try:
                    found = driver.find_elements(By.CSS_SELECTOR, selector)
                    elements.extend([el for el in found if el.is_displayed()])
                except:
                    continue
            
            for el in elements[:5]:
                try:
                    el.click()
                    time.sleep(0.5)
                    return True
                except:
                    continue
            
            return False
        except Exception as e:
            return False

    def solve_captcha(self, driver, instruction_text):
        captcha_type = self.detect_captcha_type(instruction_text, driver)
        log_info(0, f"Detected captcha type: {captcha_type}")
        
        solvers = {
            'rotation_match': self.solve_rotation_match,
            'letter_match': self.solve_letter_match,
            'color_match': self.solve_color_match,
            'sequence_match': self.solve_sequence_match,
            'slide_game': self.solve_slide_captcha,
            'unknown': self.solve_unknown_captcha,
        }
        
        solver = solvers.get(captcha_type, self.solve_unknown_captcha)
        
        if captcha_type == 'slide_game':
            return solver(driver)
        else:
            return solver(driver, instruction_text)

# ============ Main Captcha Flow ============

def get_instruction_text(driver):
    try:
        instruction_selectors = [
            "div[class*='title']",
            "div[class*='desc']",
            "div[class*='tip']",
            "div[class*='instruction']",
            "div[class*='guide']",
            "div[class*='prompt']",
            "p[class*='tip']",
        ]
        
        for selector in instruction_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    if el.is_displayed():
                        text = el.text.strip()
                        if text and len(text) > 5:
                            return text
            except:
                continue
        
        body = driver.find_element(By.TAG_NAME, "body").text
        lines = [l.strip() for l in body.split('\n') if l.strip()]
        for line in lines:
            if '点击' in line or '拖动' in line or '请' in line:
                return line
        
        return ""
    except Exception as e:
        return ""

def extract_validate_token(driver):
    try:
        driver.switch_to.default_content()
        
        page_source = driver.page_source
        
        scripts = driver.find_elements(By.TAG_NAME, "script")
        for script in scripts:
            content = script.get_attribute("innerHTML") or ""
            if 'validate' in content:
                matches = re.findall(r'["\']validate["\']\s*:\s*["\']([^"\']+)["\']', content)
                if matches:
                    return matches[0]
                matches = re.findall(r'validate\s*=\s*["\']([^"\']+)["\']', content)
                if matches:
                    return matches[0]
        
        token = driver.execute_script("""
            try {
                return localStorage.getItem('captcha_token') || 
                       sessionStorage.getItem('captcha_token') || 
                       window.validate || 
                       window._captcha_token ||
                       null;
            } catch(e) {
                return null;
            }
        """)
        if token:
            return token
        
        cookies = driver.get_cookies()
        for cookie in cookies:
            if 'validate' in cookie['name'].lower() or 'token' in cookie['name'].lower():
                return cookie['value']
        
        try:
            validate_el = driver.find_element(By.XPATH, "//input[@name='validate']")
            if validate_el:
                return validate_el.get_attribute('value')
        except:
            pass
        
        return None
    except Exception as e:
        return None

def wait_for_solve(driver, timeout=SOLVE_TIMEOUT, browser_index=0):
    start = time.time()
    last_log = -10
    solve_attempts = 0
    max_attempts = 3
    
    solver = VisualCaptchaSolver()
    
    try:
        captcha_url = "https://cstaticdun.126.net/load.min.js"
        driver.get(captcha_url)
        time.sleep(3)
        
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        for iframe in iframes:
            try:
                driver.switch_to.frame(iframe)
                break
            except:
                continue
        
        while time.time() - start < timeout:
            elapsed = int(time.time() - start)
            if elapsed - last_log >= 8:
                log_solving(browser_index, elapsed)
                last_log = elapsed
            
            token = extract_validate_token(driver)
            if token and len(token) > 20:
                return token
            
            instruction_text = get_instruction_text(driver)
            if instruction_text:
                log_info(browser_index, f"Instruction: {instruction_text}")
            
            if solve_attempts < max_attempts:
                if solver.solve_captcha(driver, instruction_text):
                    time.sleep(1.5)
                    token = extract_validate_token(driver)
                    if token and len(token) > 20:
                        return token
                solve_attempts += 1
                time.sleep(1)
            
            try:
                success_elements = driver.find_elements(By.XPATH, 
                    "//*[contains(text(), '成功') or contains(text(), '通过') or contains(@class, 'success')]")
                if success_elements:
                    time.sleep(0.5)
                    token = extract_validate_token(driver)
                    if token and len(token) > 20:
                        return token
            except:
                pass
            
            time.sleep(0.5)
        
        return extract_validate_token(driver)
    except Exception as e:
        log_warn(browser_index, f"Solve loop error: {e}")
        return None

# ============ Shared State and Worker ============

class SharedState:
    def __init__(self, target_count, loop_forever):
        self.target_count = target_count
        self.loop_forever = loop_forever
        self.generated = 0
        self.existing = set()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def should_continue(self):
        if self.stop_event.is_set():
            return False
        if self.loop_forever:
            return True
        with self.lock:
            return self.generated < self.target_count

    def add_tokens(self, tokens):
        with self.lock:
            if tokens in self.existing:
                return False
            self.existing.add(tokens)
            self.generated += 1
            return True

def browser_worker(idx, shared, use_server):
    driver = None
    consec_fails = 0
    tok_this_br = 0
    attempt = 0

    try:
        while shared.should_continue():
            attempt += 1
            
            driver_alive = False
            if driver is not None:
                try:
                    driver.current_url
                    driver_alive = True
                except:
                    driver_alive = False
            
            need_new = (driver is None or not driver_alive or 
                       consec_fails >= MAX_CONSECUTIVE_FAILS or 
                       tok_this_br >= MAX_TOKENS_PER_BROWSER)

            if need_new:
                if driver is not None:
                    log_relaunch(idx, f"{consec_fails} fails / {tok_this_br} tokens")
                    try:
                        driver.quit()
                    except:
                        pass
                    driver = None
                    time.sleep(random.uniform(1, 3))

                log_info(idx, "Opening browser…")
                driver = create_driver(browser_index=idx)
                if not driver:
                    log_warn(idx, "Browser failed! Retry in 5s…")
                    time.sleep(5)
                    continue

                consec_fails = 0
                tok_this_br = 0

            with shared.lock:
                gen_now = shared.generated
            remaining = "∞" if shared.loop_forever else str(shared.target_count - gen_now)
            file_count = count_tokens()
            
            log_status(idx, attempt, gen_now, file_count, remaining, tok_this_br)

            token = wait_for_solve(driver, timeout=SOLVE_TIMEOUT, browser_index=idx)

            if token:
                if shared.add_tokens(token):
                    increment_tokens_count()
                    server_id = send_tokens_to_server(token, use_server)
                    tok_this_br += 1
                    consec_fails = 0
                    with shared.lock:
                        g = shared.generated
                    extra = f"  [srv:{server_id}]" if use_server and server_id else ""
                    log_success(idx, g, token, extra)
                    time.sleep(random.uniform(1, 3))
                else:
                    log_warn(idx, "Duplicate token, skip")
            else:
                consec_fails += 1
                log_fail(idx, consec_fails, MAX_CONSECUTIVE_FAILS)
                try:
                    driver.delete_all_cookies()
                except:
                    pass

    except Exception as e:
        log_warn(idx, f"Error: {e}")
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass
        log_info(idx, "Worker finished.")

def generate(target_count, loop_forever=False, use_server=False):
    shared = SharedState(target_count, loop_forever)
    threads = []

    for i in range(NUM_BROWSERS):
        t = threading.Thread(
            target=browser_worker,
            args=(i, shared, use_server),
            daemon=True,
            name=f"B{i+1}"
        )
        threads.append(t)
        t.start()
        if i < NUM_BROWSERS - 1:
            time.sleep(2)

    try:
        while any(t.is_alive() for t in threads):
            if not loop_forever and not shared.should_continue():
                shared.stop_event.set()
            time.sleep(1)
    except KeyboardInterrupt:
        cprint("\n⚠  Stopped by user (Ctrl+C)")
        shared.stop_event.set()

    for t in threads:
        try:
            t.join(timeout=10)
        except:
            pass

    return shared.generated

def _generation_worker(threads, loop_forever, use_server):
    global _generation_running, _generation_stats
    try:
        _generation_stats["status"] = "running"
        start = time.time()
        generated = generate(50, loop_forever, use_server)
        elapsed = time.time() - start
        total = count_tokens()
        
        _generation_stats["generated"] = generated
        _generation_stats["total"] = total
        _generation_stats["status"] = "completed"
        _generation_stats["elapsed"] = int(elapsed)
        
        cprint(f"[{ts()}] [INFO] Generation complete: {generated} tokens in {int(elapsed)}s")
    except Exception as e:
        _generation_stats["status"] = "error"
        _generation_stats["error"] = str(e)
        cprint(f"[{ts()}] [ERROR] {e}")
    finally:
        _generation_running = False
        kill_chrome()
        cleanup_chrome_garbage()

# ============ Flask Routes ============

@app.route('/', methods=['GET'])
def status():
    return jsonify({
        "status": _generation_stats["status"],
        "generated": _generation_stats["generated"],
        "total": _generation_stats["total"],
        "elapsed": _generation_stats.get("elapsed", 0),
        "error": _generation_stats.get("error")
    })

@app.route('/start', methods=['POST'])
def start_generation():
    global _generation_thread, _generation_running, NUM_BROWSERS
    
    if _generation_running:
        return jsonify({"error": "Generation already running"}), 400
    
    data = request.json or {}
    threads = min(data.get("threads", 1), MAX_THREADS)
    use_server = data.get("use_server", True)
    
    NUM_BROWSERS = threads
    _generation_stats["status"] = "starting"
    _generation_stats["generated"] = 0
    _generation_stats["total"] = count_tokens()
    _generation_running = True
    
    _generation_thread = threading.Thread(
        target=_generation_worker,
        args=(threads, True, use_server),
        daemon=False
    )
    _generation_thread.start()
    
    return jsonify({
        "message": "Generation started",
        "threads": threads,
        "use_server": use_server
    })

@app.route('/stop', methods=['POST'])
def stop_generation():
    global _generation_running
    _generation_running = False
    kill_chrome()
    return jsonify({"message": "Stop signal sent"})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "status": _generation_stats["status"]})

def signal_handler(sig, frame):
    cprint(f"\n[!] Received signal {sig}, shutting down...")
    global _generation_running
    _generation_running = False
    kill_chrome()
    sys.exit(0)

def main():
    # Setup signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    cprint(f"\n  🔐 NECaptcha solver")
    cprint(f"  ─────────────────────────────────────────")
    cprint(f"  Mode       : Visual Recognition + OCR")
    cprint(f"  Port       : {PORT}")
    cprint(f"  Headless   : {HEADLESS}")
    cprint(f"  Browsers   : {NUM_BROWSERS}")
    cprint(f"  Chrome     : {CHROME_BIN}")
    cprint(f"  Driver     : {CHROMEDRIVER_PATH}")
    cprint(f"  Server     : {SERVER_URL}")
    cprint(f"  ─────────────────────────────────────────")
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)

if __name__ == "__main__":
    main()