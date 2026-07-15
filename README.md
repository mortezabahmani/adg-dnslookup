# 🛡️ ADG-DNSLookup

**Anti-DNS Poisoning System for AdGuard Home**

[**فارسی (Persian)**](./README-fa.md) | **English**

---

## 📖 Philosophy & Problem Statement

In certain regions, internet service providers and governments actively monitor, filter, or manipulate DNS queries—a practice known as DNS poisoning. When you attempt to resolve a well-known domain like `google.com`, the local DNS servers may return a fake, local IP address instead of the real global IP. This can lead to blocked access, compromised security, or forced routing through monitored infrastructure.

**ADG-DNSLookup** aims to solve this problem by bypassing local DNS manipulation entirely.

### 💡 The Solution

This system uses a reliable, distributed approach to resolve DNS securely from outside the restricted network and injects the legitimate IP addresses directly into your local **AdGuard Home** installation.

1. **Automated Secure Resolution**: A GitHub Actions workflow runs every 12 hours from an unrestricted environment.
2. **Majority Vote Mechanism**: Domains are resolved using trusted DNS-over-HTTPS (DoH) providers (Cloudflare, Google, Quad9). An IP is only considered valid if at least 2 out of 3 resolvers agree.
3. **Artifact Storage**: Validated IP records are stored transparently as JSON artifacts in this repository.
4. **Client-side Injection**: A lightweight client script running on your local network fetches these validated JSON artifacts and seamlessly updates your AdGuard Home's DNS Rewrite rules.

From this point on, your local network devices will instantly resolve crucial domains to their genuine IP addresses via AdGuard Home, without querying the local, potentially poisoned, upstream DNS.

## ✨ Features & Capabilities

- **Secure & Tamper-Proof**: Uses DoH and a consensus mechanism (Majority Vote) to eliminate any single point of failure or manipulation.
- **Automated Sync**: The server-side updates every 12 hours automatically. You can automate the client script via cron jobs.
- **Multiple Modes of Operation**:
  - `Online` (Default): Pulls the latest pre-resolved IPs securely from GitHub.
  - `Standalone`: Resolves IPs locally via secure DoH (useful if GitHub itself is inaccessible but a VPN is active).
- **Extensive Built-in Lists**: Categorized domain lists out of the box (Google, Cloudflare, Meta, Microsoft, Social Media, Streaming, CDNs, Developer Tools).
- **Flexible Clients**: Includes both a Python client (for servers/NAS) and a Shell script (perfect for lightweight routers like OpenWrt).
- **Smart Diffing**: The client only updates records that have changed, preserving your custom AdGuard Home rewrites and preventing unnecessary API calls.

## 📂 Project Structure

```text
.
├── client/         # Client scripts (Python & Shell) to sync IPs to AdGuard Home
├── lists/          # Plain text domain lists categorized by service
├── resolved/       # JSON files containing the securely resolved IPs (updated via GitHub Actions)
├── resolver/       # Python scripts used by GitHub Actions to perform the DoH resolution
├── README.md       # English documentation
└── README-fa.md    # Persian documentation
```

## 🚀 Quick Start Guide

### Prerequisites
- AdGuard Home installed on your local network (e.g., router, Raspberry Pi, server).
- **Python Client**: Requires Python 3.8+
- **Shell Client (OpenWrt)**: Requires `curl` and `jq`

### 1. Installation

Clone the repository and install the required dependencies (if using the Python client):

```bash
git clone https://github.com/mortezabahmani/adg-dnslookup.git
cd adg-dnslookup
pip install -r requirements.txt
```

### 2. Configuration

Create a copy of the example configuration file:

```bash
cp client/config.example.ini client/config.ini
```

Edit `client/config.ini` with your favorite text editor:

```ini
[adguard]
url = http://192.168.1.1:3000    # Your AdGuard Home URL
username = admin                 # AdGuard Home Web Interface Username

[github]
repo = mortezabahmani/adg-dnslookup
branch = main

[lists]
# Comma-separated list of categories you want to sync
enabled = google,social,dev,cdn
```

**Security Note:** Do not put your password directly in the config file. Export it as an environment variable instead:

```bash
export AGH_PASSWORD="your_password"
```

### 3. Execution

It is recommended to do a dry-run first to preview the changes without modifying AdGuard Home:

```bash
python client/adg_sync.py --dry-run --verbose
```

If everything looks correct, apply the changes:

```bash
python client/adg_sync.py --verbose
```

### 4. Automation (Optional)

You can easily set up a cron job to keep your AdGuard Home updated automatically:

```bash
bash client/install_cron.sh
```

## 🛠 Working Modes

### Online Mode (Default)
Reads the pre-resolved IPs directly from the GitHub repository. Requires internet access to GitHub.

```bash
python client/adg_sync.py --mode online --lists google,social
```

### Standalone Mode
The script acts as the resolver itself, ignoring the GitHub repository. It resolves domains using DoH locally. This is highly useful if GitHub is blocked but you have a temporary VPN connection.

```bash
python client/adg_sync.py --mode standalone --lists google,social
```

### OpenWrt / Lightweight Mode (Shell Script)
For devices where installing Python is not feasible.

```bash
export AGH_URL="http://192.168.1.1:3000"
export AGH_USER="admin"
export AGH_PASS="password"
export REPO="mortezabahmani/adg-dnslookup"
export LISTS="google,social,dev"

bash client/adg_sync.sh
```

## ➕ Adding New Domains

You can easily contribute to the global lists or create your own custom categories:
1. Create a new `.txt` file in the `lists/` directory or edit an existing one.
2. Add your domains (one per line).
3. Submit a Pull Request!

## ❓ FAQ

**Is this safe?**
Yes. IPs are resolved via trusted providers (Cloudflare, Google, Quad9) from secure, uncompromised servers (GitHub Actions). The Majority Vote system guarantees that if one resolver is compromised or returns an error, the final IP remains accurate.

**What if GitHub is blocked?**
Use `Standalone Mode`. You can download the lists beforehand and run the resolution directly on your machine while connected to a VPN.

**Will this slow down AdGuard Home?**
No. Adding thousands of DNS Rewrites has a negligible impact on AdGuard Home's performance, as lookups are highly optimized.

**Will this overwrite my custom DNS rewrites?**
No. The script only manages the specific domains present in your enabled lists. It leaves your manual AdGuard Home entries untouched.

## 📄 License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

---
⭐ **If you find this project useful, please consider giving it a star!**
