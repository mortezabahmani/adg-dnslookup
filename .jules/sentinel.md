## 2024-07-15 - Prevent Command-Line Credential Exposure
**Vulnerability:** Command-line credential exposure when using `curl -u "user:pass"` in `client/adg_sync.sh` (CWE-214).
**Learning:** Shell scripts targeting embedded devices (like OpenWrt) often pass credentials directly in arguments to commands, exposing them to other users on the system via `ps`.
**Prevention:** Use credential files (e.g., `curl -K <config_file>`) or standard input pipelines where applicable instead of passing passwords via command line arguments.
