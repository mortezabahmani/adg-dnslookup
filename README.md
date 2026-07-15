# 🛡️ ADG-DNSLookup

**سیستم ضد-مسمومیت DNS برای AdGuard Home**

[English](#english) | [فارسی](#فارسی)

---

<a id="فارسی"></a>

## 🇮🇷 فارسی

### مشکل چیست؟

در ایران، حکومت DNS عمومی را مسدود یا دستکاری می‌کند. وقتی شما `google.com` را resolve می‌کنید، ممکن است IP جعلی دریافت کنید. این پروژه این مشکل را حل می‌کند.

### راه‌حل

1. **GitHub Actions** هر ۱۲ ساعت، دامنه‌های مهم را از طریق resolver‌های معتبر DoH (Cloudflare, Google, Quad9) resolve می‌کند
2. نتایج با **Majority Vote** اعتبارسنجی می‌شوند (۲ از ۳ resolver باید توافق داشته باشند)
3. IP‌های معتبر در فایل‌های JSON در این ریپو ذخیره می‌شوند
4. **اسکریپت کلاینت** این IP‌ها را به AdGuard Home DNS Rewrite اضافه می‌کند
5. از این به بعد، DNS شما از طریق AdGuard Home و IP‌های معتبر resolve می‌شود

### معماری

```
GitHub Actions (خارج از ایران)
    │
    ├── DoH Cloudflare ──┐
    ├── DoH Google    ───┼── Majority Vote ──► resolved/*.json
    └── DoH Quad9     ──┘
                                                    │
                                                    ▼
                                          Client (Python/Shell)
                                                    │
                                                    ▼
                                          AdGuard Home API
                                                    │
                                                    ▼
                                          DNS Rewrite Rules
```

### نصب سریع

#### پیش‌نیازها

- Python 3.8+ (برای کلاینت Python)
- یا `curl` + `jq` (برای کلاینت Shell/OpenWrt)
- AdGuard Home نصب شده روی روتر یا سرور

#### ۱. دانلود اسکریپت

```bash
# کلون ریپو
git clone https://github.com/mortezabahmani/adg-dnslookup.git
cd adg-dnslookup

# نصب وابستگی‌ها
pip install -r requirements.txt
```

#### ۲. تنظیمات

```bash
# کپی فایل نمونه
cp client/config.example.ini client/config.ini

# ویرایش تنظیمات
nano client/config.ini
```

فایل `config.ini` را ویرایش کنید:

```ini
[adguard]
url = http://192.168.1.1:3000    # آدرس AdGuard Home شما
username = admin                   # یوزرنیم

[github]
repo = mortezabahmani/adg-dnslookup
branch = main

[lists]
enabled = google,social,dev,cdn    # لیست‌هایی که می‌خواهید
```

پسورد را از طریق environment variable تنظیم کنید:

```bash
export AGH_PASSWORD="your_password"
```

#### ۳. اجرا

```bash
# اول dry-run بزنید تا ببینید چه اتفاقی می‌افتد
python client/adg_sync.py --dry-run --verbose

# اگر همه‌چیز خوب بود، اجرای واقعی
python client/adg_sync.py --verbose
```

#### ۴. اتوماسیون (اختیاری)

```bash
# نصب cron job
bash client/install_cron.sh
```

### حالت‌های کاری

#### حالت Online (پیش‌فرض)

```bash
python client/adg_sync.py --mode online --lists google,social
```

IP‌ها را از ریپوی GitHub می‌خواند. نیاز به اینترنت دارد.

#### حالت Standalone

```bash
python client/adg_sync.py --mode standalone --lists google,social
```

خودش IP‌ها را resolve می‌کند. برای وقتی که GitHub فیلتر شده و VPN دارید.

#### OpenWrt (Shell Script)

```bash
# تنظیم متغیرها
export AGH_URL="http://192.168.1.1:3000"
export AGH_USER="admin"
export AGH_PASS="password"
export REPO="mortezabahmani/adg-dnslookup"
export LISTS="google,social,dev"

# اجرا
bash client/adg_sync.sh
```

### تشخیص دستکاری DNS

در حالت Standalone، اگر DNS محلی شما IP متفاوتی برگرداند:

```
⚠️  TAMPER WARNING for google.com
    Your system DNS: 10.10.34.35
    DoH Cloudflare:  142.250.185.46
    DoH Google:      142.250.185.46
    → DNS شما احتمالاً دستکاری شده!
    → آیا IP از DoH استفاده شود؟ [Y/n]
```

### لیست‌های موجود

| فایل | توضیحات |
|------|---------|
| `google.txt` | سرویس‌های گوگل (Search, Gmail, Drive, YouTube, ...) |
| `cloudflare.txt` | سرویس‌های Cloudflare (DNS, CDN, Workers, ...) |
| `meta.txt` | Meta/Facebook/Instagram/WhatsApp/Threads |
| `microsoft.txt` | Microsoft/Office365/Azure/LinkedIn/Bing |
| `social.txt` | Telegram, Signal, Twitter/X, Discord, Reddit |
| `streaming.txt` | YouTube, Netflix, Spotify, Twitch |
| `dev.txt` | GitHub, npm, PyPI, Docker, VS Code, Brew, Conda, ... |
| `cdn.txt` | Cloudflare CDN, Fastly, Akamai, jsDelivr, ... |
| `cloud.txt` | AWS, GCP, Azure, DigitalOcean, Hetzner, ... |

### اضافه کردن دامنه جدید

فقط کافیست یک فایل `.txt` جدید در پوشه `lists/` بسازید:

```text
# My Custom List
example.com
api.example.com
```

یا به فایل‌های موجود اضافه کنید و Pull Request بزنید.

### سوالات متداول

**آیا امنه؟**
بله. IP‌ها از resolver‌های معتبر (Cloudflare, Google, Quad9) و از سرورهای GitHub Actions (خارج از ایران) resolve می‌شوند. Majority Vote تضمین می‌کند که حتی اگر یک resolver دستکاری شود، نتیجه نهایی درست باشد.

**اگر GitHub فیلتر باشه چی؟**
از حالت Standalone استفاده کنید. فایل‌های لیست را قبلاً دانلود کنید و با VPN اجرا کنید.

**AdGuard Home کند نمی‌شه؟**
تعداد معقول rewrite (حتی ۱۰۰۰+) تأثیر محسوسی روی سرعت ندارد.

**آیا rewrite‌های دستی من پاک می‌شن؟**
نه. اسکریپت فقط دامنه‌هایی که در لیست‌ها هستند را مدیریت می‌کند.

---

<a id="english"></a>

## 🌍 English

### Problem

In Iran, the government blocks or poisons public DNS servers. When you resolve `google.com`, you might get a fake IP address. This project solves that problem.

### Solution

1. **GitHub Actions** resolves important domains every 12 hours via trusted DoH resolvers (Cloudflare, Google, Quad9)
2. Results are validated with **Majority Vote** (2 of 3 resolvers must agree)
3. Valid IPs are stored as JSON files in this repository
4. A **client script** pushes these IPs to AdGuard Home DNS Rewrites
5. Your DNS now resolves through AdGuard Home with trusted IPs

### Quick Start

```bash
# Clone
git clone https://github.com/mortezabahmani/adg-dnslookup.git
cd adg-dnslookup
pip install -r requirements.txt

# Configure
cp client/config.example.ini client/config.ini
nano client/config.ini
export AGH_PASSWORD="your_password"

# Dry run first
python client/adg_sync.py --dry-run --verbose

# Apply
python client/adg_sync.py --verbose

# Automate (optional)
bash client/install_cron.sh
```

### Modes

| Mode | Command | Use When |
|------|---------|----------|
| Online | `--mode online` | GitHub is accessible |
| Standalone | `--mode standalone` | GitHub is blocked, using VPN |

### CLI Options

```
--mode {online,standalone}     Working mode (default: online)
--lists LIST                   Comma-separated list names
--dry-run                      Preview changes without applying
--verbose / -v                 Verbose output
--config PATH                  Config file path
--force                        Accept all IPs without confirmation
--non-interactive              For cron: skip suspicious, don't prompt
```

### Contributing

1. Fork this repository
2. Add domains to existing lists or create new `.txt` files in `lists/`
3. Submit a Pull Request

### How It Works

```
GitHub Actions (outside Iran) runs every 12h:
  → Reads lists/*.txt
  → Resolves via DoH (Cloudflare + Google + Quad9)
  → Majority vote validation
  → Commits resolved/*.json

Client (on your machine/router):
  → Downloads resolved/*.json from GitHub
  → Diffs with current AdGuard Home rewrites
  → Adds/removes/updates rewrites via API
```

### License

MIT License. See [LICENSE](LICENSE).

---

**⭐ اگر مفید بود، ستاره بدید!**
