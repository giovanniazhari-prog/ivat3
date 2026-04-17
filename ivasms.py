"""
iVAS SMS Client — HTTP client untuk ivasms.com
Menggunakan curl_cffi dengan Chrome impersonation untuk bypass Cloudflare.
"""
import asyncio
import io
import json
import logging
import re
import urllib.parse
from datetime import date

import aiohttp
from curl_cffi.requests import AsyncSession as CurlSession

logger = logging.getLogger(__name__)

IVASMS_BASE_URL = "https://www.ivasms.com"
SOCKET_URL = "https://ivasms.com:2087"

# ── Country name → emoji ────────────────────────────────────────────────────

_NAME_TO_EMOJI: dict[str, str] = {
    "IVORY COAST": "🇨🇮", "COTE D'IVOIRE": "🇨🇮",
    "TOGO": "🇹🇬", "NIGER": "🇳🇪", "SENEGAL": "🇸🇳",
    "MALI": "🇲🇱", "BURKINA FASO": "🇧🇫", "BENIN": "🇧🇯",
    "GUINEA": "🇬🇳", "MAURITANIA": "🇲🇷", "GAMBIA": "🇬🇲",
    "NIGERIA": "🇳🇬", "GHANA": "🇬🇭", "CAMEROON": "🇨🇲",
    "DR CONGO": "🇨🇩", "CONGO": "🇨🇬", "KENYA": "🇰🇪",
    "TANZANIA": "🇹🇿", "UGANDA": "🇺🇬", "ETHIOPIA": "🇪🇹",
    "RWANDA": "🇷🇼", "BURUNDI": "🇧🇮", "MOZAMBIQUE": "🇲🇿",
    "ZAMBIA": "🇿🇲", "ZIMBABWE": "🇿🇼", "SOUTH AFRICA": "🇿🇦",
    "MOROCCO": "🇲🇦", "ALGERIA": "🇩🇿", "TUNISIA": "🇹🇳",
    "LIBYA": "🇱🇾", "EGYPT": "🇪🇬", "MADAGASCAR": "🇲🇬",
    "SIERRA LEONE": "🇸🇱", "LIBERIA": "🇱🇷", "GUINEA-BISSAU": "🇬🇼",
    "CABO VERDE": "🇨🇻", "SAO TOME": "🇸🇹",
    "UKRAINE": "🇺🇦", "RUSSIA": "🇷🇺", "POLAND": "🇵🇱",
    "ROMANIA": "🇷🇴", "TURKEY": "🇹🇷", "GEORGIA": "🇬🇪",
    "INDONESIA": "🇮🇩", "PHILIPPINES": "🇵🇭", "VIETNAM": "🇻🇳",
    "INDIA": "🇮🇳", "PAKISTAN": "🇵🇰", "BANGLADESH": "🇧🇩",
    "THAILAND": "🇹🇭", "MALAYSIA": "🇲🇾", "MYANMAR": "🇲🇲",
    "CAMBODIA": "🇰🇭", "LAOS": "🇱🇦", "SRI LANKA": "🇱🇰",
    "NEPAL": "🇳🇵", "CHINA": "🇨🇳", "SOUTH KOREA": "🇰🇷",
    "TAIWAN": "🇹🇼", "JAPAN": "🇯🇵", "BRAZIL": "🇧🇷",
    "MEXICO": "🇲🇽", "COLOMBIA": "🇨🇴", "PERU": "🇵🇪",
    "ARGENTINA": "🇦🇷", "CHILE": "🇨🇱", "VENEZUELA": "🇻🇪",
    "ECUADOR": "🇪🇨", "BOLIVIA": "🇧🇴",
}


def _country_emoji(country_name: str) -> str:
    return _NAME_TO_EMOJI.get(country_name.upper().strip(), "🌍")


# ── Default HTTP headers ────────────────────────────────────────────────────

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

JSON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-CH-UA": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}


# ── Cookie helpers ──────────────────────────────────────────────────────────

def parse_cookies(raw: str) -> dict[str, str]:
    if not raw or not raw.strip():
        return {}
    stripped = raw.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k and v}
            if isinstance(data, list):
                return {
                    item["name"]: item["value"]
                    for item in data
                    if isinstance(item, dict) and item.get("name") and item.get("value")
                }
        except Exception:
            pass
    result = {}
    for part in stripped.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                result[k] = v
    return result


def _xsrf_header(cookies: dict[str, str]) -> str:
    raw = cookies.get("XSRF-TOKEN", "")
    return urllib.parse.unquote(raw)


# ── XLSX → TXT converter ────────────────────────────────────────────────────

def xlsx_bytes_to_numbers(data: bytes) -> list[str]:
    """
    Parse XLSX dari /portal/numbers/export
    Struktur:
      Row 1: title (skip)
      Row 2: empty (skip)
      Row 3: headers — A=Range, B=Number, C=A2P, D=P2P
      Row 4+: data   — B column = phone number (numeric)
    Returns: list of phone number strings
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.active
        numbers = []
        for row in ws.iter_rows(min_row=4, values_only=True):
            if row is None or len(row) < 2:
                continue
            val = row[1]
            if val is None:
                continue
            try:
                numbers.append(str(int(float(str(val)))))
            except (ValueError, TypeError):
                s = str(val).strip()
                if re.match(r'^\d{7,15}$', s):
                    numbers.append(s)
        wb.close()
        logger.info(f"xlsx_bytes_to_numbers: extracted {len(numbers)} numbers")
        return numbers
    except Exception as e:
        logger.error(f"xlsx_bytes_to_numbers error: {e}")
        return []


def numbers_to_txt(numbers: list[str]) -> bytes:
    return "\n".join(numbers).encode("utf-8")


# ── Login dengan credentials langsung (tanpa FlareSolverr) ──────────────────

async def login_with_credentials(email: str, password: str) -> dict | None:
    """
    Login ke ivasms.com menggunakan email dan password.
    Menggunakan curl_cffi dengan Chrome impersonation untuk bypass Cloudflare.
    
    Step 1: GET /login — ambil cf_clearance + CSRF token via curl_cffi (chrome impersonation)
    Step 2: POST /login — kirim credentials dengan cookies yang sama
    
    Returns: cookies dict kalau sukses, None kalau gagal.
    """
    # Gunakan impersonasi Chrome terbaru untuk bypass CF
    impersonate_versions = ["chrome131", "chrome124", "chrome110"]
    
    for imp in impersonate_versions:
        logger.info(f"login_with_credentials: mencoba dengan {imp}")
        result = await _try_login(email, password, imp)
        if result is not None:
            return result
        await asyncio.sleep(2)
    
    logger.error("login_with_credentials: semua impersonation gagal")
    return None


async def _try_login(email: str, password: str, impersonate: str) -> dict | None:
    """Satu percobaan login dengan impersonation tertentu."""
    sess = CurlSession(impersonate=impersonate, headers=DEFAULT_HEADERS)
    try:
        # Step 1: GET halaman login
        resp = await sess.get(
            f"{IVASMS_BASE_URL}/login",
            allow_redirects=True,
            timeout=30,
        )
        
        logger.info(f"_try_login [{impersonate}]: GET /login status={resp.status_code}")
        
        if resp.status_code not in (200, 302, 301):
            logger.warning(f"_try_login [{impersonate}]: GET /login unexpected status {resp.status_code}")
            return None
        
        html_page = resp.text
        
        # Extract CSRF token
        m = re.search(r'name="_token"\s+value="([^"]+)"', html_page)
        if not m:
            # Coba pola lain
            m = re.search(r'"_token"\s*:\s*"([^"]+)"', html_page)
        if not m:
            m = re.search(r'csrf[_-]token.*?content="([^"]+)"', html_page, re.IGNORECASE)
        
        if not m:
            logger.warning(f"_try_login [{impersonate}]: CSRF token tidak ditemukan di halaman login")
            # Mungkin CF challenge — simpan cookies saja untuk nanti
            cf_cookies = {}
            try:
                for name, value in sess.cookies.items():
                    if name and value:
                        cf_cookies[name] = value
            except Exception:
                pass
            logger.info(f"_try_login [{impersonate}]: CF cookies sejauh ini: {list(cf_cookies.keys())}")
            return None
        
        csrf_token = m.group(1)
        logger.info(f"_try_login [{impersonate}]: CSRF={csrf_token[:10]}...")
        
        # Ambil cookies dari GET response
        get_cookies_dict = {}
        try:
            for name, value in sess.cookies.items():
                if name and value:
                    get_cookies_dict[name] = value
        except Exception:
            pass
        logger.info(f"_try_login [{impersonate}]: Cookies setelah GET: {list(get_cookies_dict.keys())}")
        
        # Step 2: POST /login dengan cookies yang sudah ada
        await asyncio.sleep(1)  # Jeda kecil agar terlihat natural
        
        resp2 = await sess.post(
            f"{IVASMS_BASE_URL}/login",
            data={
                "_token": csrf_token,
                "email": email,
                "password": password,
                "remember": "on",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{IVASMS_BASE_URL}/login",
                "Origin": IVASMS_BASE_URL,
            },
            allow_redirects=True,
            timeout=30,
        )
        
        final_url = str(resp2.url)
        logger.info(f"_try_login [{impersonate}]: POST /login → url={final_url} status={resp2.status_code}")
        
        if "/login" in final_url:
            # Cek apakah ada pesan error di halaman
            body = resp2.text
            if "password" in body.lower() and ("invalid" in body.lower() or "incorrect" in body.lower() or "salah" in body.lower()):
                logger.error(f"_try_login [{impersonate}]: credentials tidak valid")
            else:
                logger.warning(f"_try_login [{impersonate}]: masih di /login — kemungkinan CF masih blok")
            return None
        
        # Kumpulkan semua cookies
        result: dict[str, str] = {}
        try:
            for name, value in sess.cookies.items():
                if name and value:
                    result[name] = value
        except Exception:
            pass
        
        if not result:
            logger.error(f"_try_login [{impersonate}]: login berhasil tapi tidak ada cookies!")
            return None
        
        logger.info(f"_try_login [{impersonate}]: sukses! cookies={list(result.keys())}")
        return result
        
    except Exception as e:
        logger.error(f"_try_login [{impersonate}]: error: {e}")
        return None
    finally:
        try:
            sess.close()
        except Exception:
            pass


# ── iVAS HTTP Client ─────────────────────────────────────────────────────────

class IVASMSClient:
    def __init__(self, cookies_raw: str):
        self.cookies: dict[str, str] = parse_cookies(cookies_raw)
        self.csrf_token: str | None = None
        self.session: CurlSession | None = None

    async def open(self):
        if self.session and not self.session.closed:
            return
        self.session = CurlSession(impersonate="chrome131", headers=DEFAULT_HEADERS)
        self._apply_cookies()

    async def close(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        self.csrf_token = None

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()

    def _apply_cookies(self):
        if self.session:
            self.session.cookies.update(self.cookies)

    def get_updated_cookies_str(self) -> str:
        if not self.session:
            return json.dumps(self.cookies) if self.cookies else ""
        merged = dict(self.cookies)
        try:
            for name, value in self.session.cookies.items():
                if name and value:
                    merged[name] = value
        except Exception:
            pass
        return json.dumps(merged) if merged else ""

    async def login(self) -> bool:
        """
        Verifikasi cookies dengan mengakses halaman portal.
        Menggunakan curl_cffi dengan Chrome impersonation.
        """
        if not self.session:
            await self.open()

        try:
            resp = await self.session.get(
                f"{IVASMS_BASE_URL}/portal/sms/received",
                allow_redirects=True,
                timeout=30,
            )
            
            final_url = str(resp.url)
            status = resp.status_code
            html = resp.text
            
            logger.info(f"login: GET /portal/sms/received → url={final_url} status={status}")
            
            # Update cookies dari response
            try:
                for name, value in self.session.cookies.items():
                    if name and value:
                        self.cookies[name] = value
            except Exception:
                pass
            self._apply_cookies()
            
            if "/login" in final_url or status in (301, 302, 403):
                logger.error(f"login: diredirect ke /login atau 403 — cookies invalid/expired")
                return False
            
            # Cari CSRF token
            m = re.search(r'name="_token"\s+value="([^"]+)"', html)
            if m:
                self.csrf_token = m.group(1)
                logger.info(f"login: sukses! CSRF={self.csrf_token[:10]}...")
                return True
            
            # Halaman berhasil diload tapi tidak ada CSRF — cek apakah memang portal
            if "portal" in final_url.lower() or "dashboard" in html.lower():
                # Coba ambil CSRF dari endpoint lain
                logger.warning("login: CSRF tidak ditemukan, mencoba halaman dashboard...")
                resp2 = await self.session.get(
                    f"{IVASMS_BASE_URL}/portal/dashboard",
                    allow_redirects=True,
                    timeout=20,
                )
                html2 = resp2.text
                m2 = re.search(r'name="_token"\s+value="([^"]+)"', html2)
                if m2:
                    self.csrf_token = m2.group(1)
                    logger.info("login: CSRF dari dashboard OK")
                    return True
                # Tetap anggap login berhasil jika tidak di halaman login
                if "/login" not in str(resp2.url):
                    logger.info("login: session valid (tanpa CSRF — fitur yang butuh POST akan terbatas)")
                    return True
            
            logger.error("login: CSRF tidak ditemukan dan tidak ada portal page")
            return False
            
        except Exception as e:
            logger.error(f"login: {e}")
            return False

    async def keepalive(self) -> bool:
        """
        Ping /portal/dashboard untuk jaga session tetap hidup.
        Panggil tiap 20 menit supaya session tidak expired.
        Returns True kalau session masih valid.
        """
        try:
            resp = await self.session.get(
                f"{IVASMS_BASE_URL}/portal/dashboard",
                allow_redirects=True,
                timeout=15,
            )
            html = resp.text
            if "/login" in str(resp.url):
                logger.warning("keepalive: session expired (redirected to login)")
                return False
            try:
                for name, value in self.session.cookies.items():
                    if name and value:
                        self.cookies[name] = value
            except Exception:
                pass
            m = re.search(r'name="_token"\s+value="([^"]+)"', html)
            if m:
                self.csrf_token = m.group(1)
            logger.info("keepalive: session OK, cookies updated")
            return True
        except Exception as e:
            logger.error(f"keepalive: {e}")
            return False

    # ── Scan WA-active ranges dari SMS Test History ──────────────────────────

    async def get_wa_active_ranges(self, limit: int = 2000) -> list[dict]:
        """
        Scan /portal/sms/test/sms?search=WhatsApp
        Returns top ranges sorted by WhatsApp SMS count (desc).
        """
        xsrf = _xsrf_header(self.cookies)
        params = {
            "draw": "1", "start": "0", "length": str(limit),
            "columns[0][data]": "range", "columns[0][name]": "range",
            "columns[1][data]": "termination.test_number",
            "columns[1][name]": "termination.test_number",
            "columns[2][data]": "originator", "columns[2][name]": "originator",
            "columns[3][data]": "messagedata", "columns[3][name]": "messagedata",
            "columns[4][data]": "senttime", "columns[4][name]": "senttime",
            "order[0][column]": "4", "order[0][dir]": "desc",
            "search[value]": "WhatsApp", "search[regex]": "false",
        }
        url = (
            f"{IVASMS_BASE_URL}/portal/sms/test/sms?"
            + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        )
        hdrs = {
            **JSON_HEADERS,
            "X-XSRF-TOKEN": xsrf,
            "Referer": f"{IVASMS_BASE_URL}/portal/sms/test/sms",
        }
        try:
            resp = await self.session.get(
                url, headers=hdrs, timeout=30
            )
            if resp.status_code != 200:
                logger.error(f"get_wa_active_ranges: HTTP {resp.status_code}")
                return []
            data = resp.json()
        except Exception as e:
            logger.error(f"get_wa_active_ranges error: {e}")
            return []

        rows = data.get("data", [])
        total_filtered = data.get("recordsFiltered", 0)
        logger.info(f"get_wa_active_ranges: {len(rows)} rows fetched, total_filtered={total_filtered}")

        range_map: dict[str, dict] = {}
        for r in rows:
            term_id = r.get("termination_id")
            rng_name = r.get("range", "")
            sent = r.get("senttime", "")
            if not term_id or not rng_name:
                continue
            key = str(term_id)
            if key not in range_map:
                m = re.match(r'^(.*?)\s+(\d+)\s*$', rng_name.strip())
                range_map[key] = {
                    "range": rng_name.strip(),
                    "termination_id": term_id,
                    "country": m.group(1).strip().title() if m else rng_name.strip(),
                    "range_num": m.group(2) if m else rng_name.strip(),
                    "count": 0,
                    "last_seen": sent,
                }
            range_map[key]["count"] += 1
            if sent > range_map[key]["last_seen"]:
                range_map[key]["last_seen"] = sent

        result = sorted(range_map.values(), key=lambda x: x["count"], reverse=True)
        logger.info(f"Found {len(result)} unique WA-active ranges")
        return result

    # ── Add range ke My Numbers ──────────────────────────────────────────────

    async def add_range(self, termination_id: int | str, retry_on_429: int = 3) -> dict:
        """
        POST /portal/numbers/termination/number/add
        Returns {ok: bool, message: str}
        """
        if not self.csrf_token:
            return {"ok": False, "message": "No CSRF token — login() dulu"}
        payload = {"_token": self.csrf_token, "id": str(termination_id)}
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{IVASMS_BASE_URL}/portal/sms/test/sms",
        }
        for attempt in range(retry_on_429 + 1):
            try:
                resp = await self.session.post(
                    f"{IVASMS_BASE_URL}/portal/numbers/termination/number/add",
                    data=payload, headers=hdrs,
                    timeout=15,
                )
                if resp.status_code == 429:
                    wait = 5 * (attempt + 1)
                    logger.warning(f"add_range: 429 rate limit, tunggu {wait}s (attempt {attempt+1}/{retry_on_429+1})")
                    if attempt < retry_on_429:
                        await asyncio.sleep(wait)
                        continue
                    return {"ok": False, "message": "HTTP 429 (rate limited, max retry)"}
                if resp.status_code != 200:
                    return {"ok": False, "message": f"HTTP {resp.status_code}"}
                j = resp.json()
                msg = j.get("message", "OK")
                ok = "error" not in msg.lower()
                return {"ok": ok, "message": msg}
            except Exception as e:
                logger.error(f"add_range({termination_id}): {e}")
                return {"ok": False, "message": str(e)}
        return {"ok": False, "message": "Max retry exceeded"}

    async def bulk_return_all(self) -> dict:
        """
        POST /portal/numbers/return/allnumber/bluck
        Return semua nomor di My Numbers sekaligus.
        """
        if not self.csrf_token:
            return {"ok": False, "count": 0, "message": "No CSRF token — login() dulu"}
        xsrf = _xsrf_header(self.cookies)
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "X-XSRF-TOKEN": xsrf,
            "Referer": f"{IVASMS_BASE_URL}/portal/numbers",
        }
        payload = {"_token": self.csrf_token}
        try:
            resp = await self.session.post(
                f"{IVASMS_BASE_URL}/portal/numbers/return/allnumber/bluck",
                data=payload, headers=hdrs,
                timeout=30,
            )
            if resp.status_code != 200:
                return {"ok": False, "count": 0, "message": f"HTTP {resp.status_code}"}
            j = resp.json()
            msg   = j.get("message", "")
            count = j.get("count", 0)
            ok    = "successfully" in msg.lower() or count > 0
            return {"ok": ok, "count": count, "message": msg}
        except Exception as e:
            logger.error(f"bulk_return_all: {e}")
            return {"ok": False, "count": 0, "message": str(e)}

    # ── Download My Numbers XLSX ─────────────────────────────────────────────

    async def download_xlsx(self) -> bytes | None:
        """GET /portal/numbers/export → XLSX bytes"""
        try:
            resp = await self.session.get(
                f"{IVASMS_BASE_URL}/portal/numbers/export",
                headers={"Referer": f"{IVASMS_BASE_URL}/portal/numbers"},
                timeout=60,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                logger.error(f"download_xlsx: HTTP {resp.status_code}")
                return None
            data = resp.content
            if len(data) < 100:
                logger.warning(f"download_xlsx: terlalu kecil ({len(data)} bytes)")
                return None
            logger.info(f"download_xlsx: {len(data)} bytes OK")
            return data
        except Exception as e:
            logger.error(f"download_xlsx: {e}")
            return None

    # ── Get My Numbers count ─────────────────────────────────────────────────

    async def get_my_numbers_count(self) -> int:
        """Ambil total My Numbers dari DataTable."""
        xsrf = _xsrf_header(self.cookies)
        params = (
            "draw=1&start=0&length=1"
            "&columns[0][data]=number_id&columns[0][name]=id"
            "&columns[1][data]=Number&columns[1][name]=number"
            "&columns[2][data]=range&columns[2][name]=range"
            "&search[value]=&search[regex]=false"
        )
        try:
            resp = await self.session.get(
                f"{IVASMS_BASE_URL}/portal/numbers?{params}",
                headers={**JSON_HEADERS, "X-XSRF-TOKEN": xsrf},
                timeout=15,
            )
            if resp.status_code != 200:
                return -1
            d = resp.json()
            return int(d.get("recordsTotal", -1))
        except Exception as e:
            logger.error(f"get_my_numbers_count: {e}")
            return -1

    # ── Ambil socket params untuk Live SMS Monitor ───────────────────────────

    async def get_live_sms_socket_params(self) -> dict | None:
        """
        Fetch /portal/live/my_sms, extract params untuk connect socket.io.
        Returns None kalau gagal.
        """
        try:
            resp = await self.session.get(
                f"{IVASMS_BASE_URL}/portal/live/my_sms",
                timeout=20,
            )
            if resp.status_code != 200:
                logger.error(f"get_live_sms_socket_params: HTTP {resp.status_code}")
                return None
            html = resp.text
        except Exception as e:
            logger.error(f"get_live_sms_socket_params: {e}")
            return None

        m_token = re.search(r"token:\s*'([^']+)'", html)
        if not m_token:
            logger.error("get_live_sms_socket_params: token not found")
            return None

        m_user = re.search(r'user:\s*"([a-f0-9]{32})"', html)
        if not m_user:
            logger.error("get_live_sms_socket_params: user hash not found")
            return None

        m_event = re.search(
            r'liveSMSSocket\.on\("([A-Za-z0-9+/]+=*)"',
            html,
        )
        if not m_event:
            logger.error("get_live_sms_socket_params: event name not found")
            return None

        params = {
            "token": m_token.group(1),
            "user": m_user.group(1),
            "event_name": m_event.group(1),
        }
        logger.info(f"Live SMS socket params OK (user={params['user'][:8]}...)")
        return params

    # ── Received SMS history (HTTP polling fallback) ─────────────────────────

    async def get_received_sms_today(self) -> list[dict]:
        """
        POST /portal/sms/received/getsms with today's date.
        Returns list of {number, originator, message, time}.
        """
        if not self.csrf_token:
            return []
        today = date.today().strftime("%Y-%m-%d")
        payload = {"from": today, "to": today, "_token": self.csrf_token}
        hdrs = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{IVASMS_BASE_URL}/portal/sms/received",
        }
        try:
            resp = await self.session.post(
                f"{IVASMS_BASE_URL}/portal/sms/received/getsms",
                data=payload, headers=hdrs,
                timeout=20,
            )
            if resp.status_code != 200:
                logger.error(f"get_received_sms_today: HTTP {resp.status_code}")
                return []
            html = resp.text
            sms_list = []
            rows = re.findall(
                r'<tr[^>]*>([\s\S]*?)</tr>',
                html,
                re.IGNORECASE,
            )
            for row in rows:
                cells = re.findall(r'<td[^>]*>([\s\S]*?)</td>', row, re.IGNORECASE)
                if len(cells) >= 3:
                    def strip_tags(s):
                        return re.sub(r'<[^>]+>', '', s).strip()
                    sms_list.append({
                        "number": strip_tags(cells[0]),
                        "originator": strip_tags(cells[1]) if len(cells) > 1 else "",
                        "message": strip_tags(cells[2]) if len(cells) > 2 else "",
                        "time": strip_tags(cells[3]) if len(cells) > 3 else "",
                    })
            return sms_list
        except Exception as e:
            logger.error(f"get_received_sms_today: {e}")
            return []
