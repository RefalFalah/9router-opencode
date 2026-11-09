#!/usr/bin/env python3
"""Hubungkan OpenCode V2 ke 9Router jarak jauh. Python 3.9+, tanpa paket tambahan."""

import argparse
import copy
import errno
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import tempfile
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import warnings


KEY_FILENAME = "9router-api-key"
STATE_FILENAME = "9router-setup-state.json"
USER_AGENT = "9router-opencode/1.0"
DEFAULT_TIMEOUT = 30


class SetupError(Exception):
    """Kesalahan yang aman ditampilkan tanpa membocorkan API key."""


def parse_jsonc(text):
    """Hapus komentar dan trailing comma, tetapi jangan ubah isi string."""
    token = r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*[\s\S]*?\*/'

    def remove_comment(match):
        if match.group(1) is not None:
            return match.group(1)
        return re.sub(r"[^\r\n]", " ", match.group(0))

    clean = re.sub(token, remove_comment, text)
    clean = re.sub(
        r'("(?:\\.|[^"\\])*")|,(\s*[}\]])',
        lambda match: match.group(1) if match.group(1) is not None else match.group(2),
        clean,
    )

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SetupError("Konfigurasi memiliki key duplikat; rapikan dahulu.")
            result[key] = value
        return result

    def invalid_constant(value):
        raise SetupError("Konfigurasi mengandung angka yang bukan JSON valid.")

    try:
        value = json.loads(clean, object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except json.JSONDecodeError as error:
        raise SetupError(
            f"Konfigurasi bukan JSON/JSONC valid (baris {error.lineno}). Tidak diubah."
        ) from None
    if not isinstance(value, dict):
        raise SetupError("Konfigurasi harus berupa object JSON. Tidak diubah.")
    return value


def config_location(directory=None):
    if directory is None:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        directory = root / "opencode"
    directory = Path(directory).expanduser().resolve()
    candidates = [directory / "opencode.json", directory / "opencode.jsonc"]
    existing = [path for path in candidates if path.exists()]
    if len(existing) > 1:
        raise SetupError(
            "Ada opencode.json dan opencode.jsonc sekaligus. "
            "Gabungkan dahulu agar konfigurasi yang diedit tidak ambigu."
        )
    return existing[0] if existing else candidates[0]


def normalize_url(value, allow_http=False):
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise SetupError("URL tidak boleh mengandung spasi, baris baru, atau backslash.")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # Validasi format port juga.
    except ValueError:
        raise SetupError("Format URL atau port tidak valid.") from None
    if parts.scheme not in ("https", "http") or not hostname:
        raise SetupError("Gunakan URL lengkap, misalnya https://router.example.com/v1.")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise SetupError("URL tidak boleh memuat password, query, atau fragment.")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower() == "localhost"
    if parts.scheme == "http" and not loopback and not allow_http:
        raise SetupError(
            "URL remote harus HTTPS agar API key terenkripsi. "
            "Untuk LAN/VPN tepercaya saja, gunakan --allow-http."
        )
    path = parts.path.rstrip("/")
    if path.endswith(("/dashboard", "/models", "/chat/completions", "/responses")):
        raise SetupError("Masukkan URL dasar API, bukan halaman dashboard atau endpoint model.")
    if not path.endswith("/v1"):
        path += "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Jangan teruskan Authorization ke halaman login atau host lain.
        return None


def http_error_message(status, body):
    # Baca hanya kode error yang dikenal. Jangan tampilkan body mentah: bisa berisi rahasia.
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        payload = None
    if (
        status == 403
        and isinstance(payload, dict)
        and payload.get("cloudflare_error") is True
        and payload.get("error_code") == 1010
    ):
        return (
            "Cloudflare Error 1010: identitas HTTP (User-Agent) ditolak sebelum mencapai 9Router. "
            "Gunakan script terbaru dengan User-Agent aplikasi. Jika masih ditolak, "
            "hubungi pengelola tunnel untuk mengizinkan klien API; jangan nonaktifkan autentikasi."
        )
    if 300 <= status < 400:
        return "Tunnel melakukan redirect. Gunakan URL API final tanpa halaman login."
    return {
        401: "API key ditolak. Periksa key di dashboard 9Router.",
        403: "Akses ditolak. Periksa izin API key dan kebijakan akses tunnel; 403 saja belum memastikan penyebabnya.",
        404: "Endpoint /models tidak ditemukan. Periksa URL API dan path /v1.",
        429: "Terlalu banyak request. Tunggu lalu coba lagi.",
    }.get(status, "Periksa status 9Router dan tunnel.")


def network_error_message(error, timeout=DEFAULT_TIMEOUT, stage=None):
    reason = error
    for _ in range(4):
        if not isinstance(reason, URLError):
            break
        reason = reason.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        code = getattr(reason, "verify_code", None)
        label = f" (verify_code={code})" if isinstance(code, int) else ""
        return (
            "TLS_CERTIFICATE_VERIFY_FAILED" + label + ": sertifikat HTTPS tidak bisa diverifikasi. "
            "Periksa CA certificates, tanggal/jam server, dan sertifikat tunnel/proxy. "
            "Jangan matikan verifikasi TLS."
        )
    if isinstance(reason, socket.gaierror):
        return "DNS_ERROR: resolusi hostname gagal di komputer ini. Periksa DNS dan koneksi jaringan."
    if isinstance(reason, TimeoutError) or (isinstance(reason, str) and reason.lower() in ("timed out", "timeout")):
        location = f" {stage}" if stage else " saat koneksi/pembacaan respons"
        suggestion = (
            "Untuk uji batas tunggu lebih lama, gunakan --timeout 120."
            if timeout < 120
            else "Menambah timeout tidak memperbaiki endpoint yang macet; periksa juga log server dan tunnel."
        )
        return (
            f"TIMEOUT: batas tunggu I/O {timeout} detik tercapai{location}. "
            "Jika request tanpa key cepat tetapi pengecekan model ber-key lambat, periksa katalog provider/log 9Router. "
            + suggestion
        )
    if isinstance(reason, ssl.SSLError):
        code = getattr(reason, "reason", "")
        label = f" ({code})" if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]+", code) else ""
        return "TLS_ERROR" + label + ": handshake/koneksi TLS gagal. Periksa versi OpenSSL dan tunnel/proxy."
    if isinstance(reason, str):
        proxy_status = re.search(r"Tunnel connection failed:\s*(\d{3})\b", reason)
        if proxy_status:
            return "PROXY_ERROR: proxy menolak koneksi HTTPS (HTTP " + proxy_status.group(1) + "). Periksa konfigurasi proxy."
    code = getattr(reason, "errno", None)
    label = errno.errorcode.get(code, "") if isinstance(code, int) else ""
    hints = {
        "ECONNREFUSED": "koneksi ditolak oleh tujuan/proxy",
        "ECONNRESET": "koneksi diputus oleh tujuan/proxy",
        "ENETUNREACH": "rute jaringan tidak tersedia",
        "EHOSTUNREACH": "host tidak bisa dijangkau",
    }
    if label:
        return f"NETWORK_ERROR ({label}): {hints.get(label, 'koneksi gagal')}. Periksa jaringan, tunnel, dan proxy."
    # Jangan tampilkan str(error): bisa mengandung URL proxy beserta password atau API key.
    return "NETWORK_ERROR: koneksi ke 9Router gagal. Bandingkan hasil --diagnose dan curl dari komputer yang sama."


def diagnose_connection(base_url, timeout=DEFAULT_TIMEOUT):
    print("Diagnostik koneksi (tanpa API key, tanpa menyimpan konfigurasi).")
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print("TLS: " + ssl.OPENSSL_VERSION)
    proxy_names = [name for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if os.environ.get(name)]
    print("Proxy environment: " + (", ".join(proxy_names) if proxy_names else "tidak terdeteksi"))
    print("Nilai proxy/kredensial tidak ditampilkan. Verifikasi HTTPS tetap aktif.")
    print(f"Batas tunggu I/O HTTP: {timeout} detik.")
    request = Request(base_url + "/models", headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            print(f"HTTP {response.status}: header respons diterima; ini belum memvalidasi API key atau chat.")
        return 0
    except HTTPError as error:
        status = error.code
        try:
            body = error.read(8192)
        except OSError:
            body = b""
        finally:
            error.close()
        if status == 401:
            print("HTTP 401: endpoint bisa dijangkau, tetapi request tanpa autentikasi ditolak (wajar untuk API terproteksi).")
            print("Setup manual bisa dilanjutkan. Validitas key/model baru teruji saat OpenCode mengirim request.")
            return 0
        print(f"HTTP {status}: {http_error_message(status, body)}")
        return 1
    except (URLError, TimeoutError, OSError) as error:
        print(network_error_message(error, timeout, "sebelum header HTTP diterima"))
        return 1


def fetch_models(base_url, api_key, timeout=DEFAULT_TIMEOUT):
    request = Request(
        base_url + "/models",
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    stage = "sebelum header HTTP diterima"
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            stage = "saat membaca body daftar model"
            # Batasi respons salah konfigurasi; jangan cetak body yang mungkin sensitif.
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise SetupError("Respons daftar model terlalu besar.")
        data = json.loads(raw)
    except HTTPError as error:
        status = error.code
        try:
            error_body = error.read(8192)
        except OSError:
            error_body = b""
        finally:
            error.close()
        raise SetupError(f"HTTP {status}: {http_error_message(status, error_body)}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise SetupError(network_error_message(error, timeout, stage)) from None
    except (ValueError, UnicodeError):
        raise SetupError("Respons bukan JSON. URL mungkin mengarah ke dashboard/login tunnel.") from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise SetupError("Respons /models bukan format OpenAI-compatible.")
    models = sorted({
        item["id"] for item in data["data"]
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    })
    if not models:
        raise SetupError("9Router tidak mengembalikan model. Periksa provider dan izin API key.")
    return models


def object_field(parent, name):
    value = parent.setdefault(name, {})
    if not isinstance(value, dict):
        raise SetupError(f"Field {name} di konfigurasi bukan object. Tidak diubah.")
    return value


def make_config(current, base_url, model, key_path, context=None, output=None):
    config = copy.deepcopy(current)
    config.setdefault("$schema", "https://opencode.ai/config.json")
    provider = object_field(object_field(config, "providers"), "9router")
    provider.setdefault("name", "9Router")
    provider["package"] = "@opencode/ai/providers/openai-compatible"
    headers = object_field(provider, "headers")
    if not any(name.lower() == "user-agent" for name in headers):
        headers["User-Agent"] = USER_AGENT
    settings = object_field(provider, "settings")
    settings.update({"baseURL": base_url, "apiKey": "{file:" + key_path.as_posix() + "}"})
    entry = object_field(object_field(provider, "models"), model)
    entry.setdefault("name", model)
    entry["modelID"] = model
    entry["disabled"] = False
    if context is not None or output is not None:
        limit = object_field(entry, "limit")
        if context is not None:
            limit["context"] = context
        if output is not None:
            limit["output"] = output
        if isinstance(limit.get("context"), (int, float)) and isinstance(limit.get("output"), (int, float)):
            if limit["output"] > limit["context"]:
                raise SetupError("Batas output tidak boleh lebih besar dari context.")
    config["model"] = "9router/" + model
    return config


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".9router-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def encode_json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def backup_config(path):
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = path.with_name(path.name + ".bak-9router-" + stamp)
        atomic_write(backup, path.read_bytes())
        return backup
    return None


def snapshot(mapping, name):
    if name in mapping:
        return {"present": True, "value": copy.deepcopy(mapping[name])}
    return {"present": False}


def provider_snapshot(config):
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise SetupError("Field providers bukan object. Tidak diubah.")
    return snapshot(providers, "9router")


def restore_snapshot(mapping, name, saved):
    if saved["present"]:
        mapping[name] = copy.deepcopy(saved["value"])
    else:
        mapping.pop(name, None)


def read_state(path):
    state_path = path.parent / STATE_FILENAME
    if not state_path.exists():
        return None
    state = parse_jsonc(state_path.read_text(encoding="utf-8-sig"))
    valid = (
        state.get("version") == 1
        and state.get("config_name") == path.name
        and isinstance(state.get("created_providers"), bool)
        and isinstance(state.get("key_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", state["key_sha256"]) is not None
    )
    for field in ("before_provider", "after_provider", "before_model", "after_model"):
        slot = state.get(field)
        valid = valid and isinstance(slot, dict)
        if isinstance(slot, dict):
            valid = valid and isinstance(slot.get("present"), bool)
            valid = valid and (not slot.get("present") or "value" in slot)
    if not valid:
        raise SetupError("State setup tidak valid/tidak cocok. Tidak ada file yang diubah.")
    if not state["after_provider"]["present"] or not state["after_model"]["present"]:
        raise SetupError("State setup tidak lengkap. Tidak ada file yang diubah.")
    return state


def check_owned_key(path, state):
    key_path = path.parent / KEY_FILENAME
    if key_path.exists() and hashlib.sha256(key_path.read_bytes()).hexdigest() != state["key_sha256"]:
        raise SetupError(
            "File key berubah di luar script. Dibiarkan utuh; periksa dahulu sebelum setup/cleanup."
        )


def save_config(path, config, api_key, expected=None):
    current = parse_jsonc(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if expected is not None and current != expected:
        raise SetupError("Konfigurasi berubah selama setup. Jalankan ulang; perubahan baru tidak ditimpa.")
    key_path = path.parent / KEY_FILENAME
    state_path = path.parent / STATE_FILENAME
    state = read_state(path)
    if state is None:
        if key_path.exists():
            raise SetupError(
                "File key sudah ada tetapi belum ada state kepemilikan. "
                "Tidak ditimpa. Jika ini setup versi lama, lihat panduan migrasi di README."
            )
        state = {
            "version": 1,
            "config_name": path.name,
            "created_providers": "providers" not in current,
            "before_provider": provider_snapshot(current),
            "before_model": snapshot(current, "model"),
        }
    else:
        if provider_snapshot(current) != state["after_provider"]:
            raise SetupError("Provider 9Router diubah di luar script. Tidak ditimpa; periksa state/backup.")
        check_owned_key(path, state)
        if snapshot(current, "model") != state["after_model"]:
            # Pilihan default baru milik pengguna menjadi baseline untuk setup berikutnya.
            state["before_model"] = snapshot(current, "model")
    state["after_provider"] = provider_snapshot(config)
    state["after_model"] = snapshot(config, "model")
    state["key_sha256"] = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    encoded = encode_json(config)
    encoded_state = encode_json(state)
    backup = backup_config(path)
    old_key = key_path.read_bytes() if key_path.exists() else None
    old_state = state_path.read_bytes() if state_path.exists() else None
    try:
        atomic_write(key_path, api_key.encode("utf-8"))
        atomic_write(state_path, encoded_state)
        atomic_write(path, encoded)
    except OSError:
        for artifact, previous in ((key_path, old_key), (state_path, old_state)):
            if previous is None:
                artifact.unlink(missing_ok=True)
            else:
                atomic_write(artifact, previous)
        raise
    return backup


def uses_key_file(value, key_path, config_directory):
    if isinstance(value, dict):
        return any(uses_key_file(item, key_path, config_directory) for item in value.values())
    if isinstance(value, list):
        return any(uses_key_file(item, key_path, config_directory) for item in value)
    if isinstance(value, str):
        for reference in re.findall(r"\{file:([^}]+)\}", value):
            reference_path = Path(reference).expanduser()
            if not reference_path.is_absolute():
                reference_path = config_directory / reference_path
            if os.path.normcase(reference_path.resolve()) == os.path.normcase(key_path.resolve()):
                return True
    return False


def cleanup(path, dry_run=False, yes=False):
    state = read_state(path)
    if state is None:
        print("Tidak ada state setup yang dikelola script. Tidak ada file yang dihapus.")
        print("Konfigurasi/key versi lama atau buatan manual tidak diambil alih otomatis. Lihat README.")
        return 0
    raw_config = path.read_bytes() if path.exists() else None
    current = parse_jsonc(raw_config.decode("utf-8-sig")) if raw_config is not None else {}
    actual_provider = provider_snapshot(current)
    if actual_provider not in (state["after_provider"], state["before_provider"], {"present": False}):
        raise SetupError(
            "Provider 9Router diubah di luar script. Cleanup dihentikan tanpa perubahan; "
            "periksa state/backup agar konfigurasi manual tidak terhapus."
        )
    check_owned_key(path, state)
    result = copy.deepcopy(current)
    if actual_provider == state["after_provider"]:
        restore_snapshot(result["providers"], "9router", state["before_provider"])
        if state["created_providers"] and not result["providers"]:
            del result["providers"]
    if snapshot(current, "model") == state["after_model"]:
        restore_snapshot(result, "model", state["before_model"])
        model_action = "dipulihkan ke kondisi sebelum setup"
    else:
        model_action = "pilihan sekarang dipertahankan (bukan default terakhir buatan script)"
    key_path = path.parent / KEY_FILENAME
    keep_key = uses_key_file(result, key_path, path.parent)
    print("Rencana cleanup lokal:")
    print("- Perubahan provider 9Router buatan script dibatalkan; provider sebelumnya dipulihkan bila ada.")
    print("- Model default: " + model_action + ".")
    print("- File key: " + ("dipertahankan karena masih dirujuk konfigurasi global." if keep_key else "dihapus jika ada."))
    print("- State setup: " + ("disimpan agar penghapusan key bisa dicoba lagi nanti." if keep_key else "dihapus setelah cleanup selesai."))
    print("- Pengaturan lain, riwayat OpenCode, server asal, tunnel, dan backup tidak dihapus.")
    if dry_run:
        print("Preview saja (--dry-run). Tidak ada file yang diubah.")
        return 0
    if not yes and input("Lanjutkan? Ketik CLEANUP: ").strip() != "CLEANUP":
        print("Cleanup dibatalkan. Tidak ada file yang diubah.")
        return 0
    if (path.read_bytes() if path.exists() else None) != raw_config or read_state(path) != state:
        raise SetupError("Konfigurasi/state berubah selama konfirmasi. Tidak diubah; ulangi cleanup.")
    check_owned_key(path, state)
    if result != current:
        backup = backup_config(path)
        atomic_write(path, encode_json(result))
        if backup:
            print(f"Backup sebelum cleanup: {backup}")
    if not keep_key:
        key_path.unlink(missing_ok=True)
        (path.parent / STATE_FILENAME).unlink()
    print("Cleanup selesai. Tidak ada pencabutan API key di server; cabut lewat dashboard jika perlu.")
    if keep_key:
        print("Key masih dipakai. Setelah referensinya dilepas, jalankan --cleanup lagi.")
    print("OpenCode memuat ulang konfigurasi otomatis; sesi yang memakai model lama perlu /models.")
    return 0


def prompt(label, default=""):
    suffix = f" [{default}]" if default else ""
    return input(label + suffix + ": ").strip() or default


def positive_integer(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Harus berupa bilangan bulat positif.") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("Harus berupa bilangan bulat positif.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="URL tunnel, boleh dengan atau tanpa /v1")
    parser.add_argument("--model", help="ID model persis seperti di 9Router, termasuk prefix provider")
    parser.add_argument("--list-models", action="store_true", help="Lihat ID model tanpa menyimpan konfigurasi")
    parser.add_argument("--check-model", action="store_true", help="Opsional: ambil daftar /models untuk mengecek ID model sebelum menyimpan")
    parser.add_argument("--config-dir", help="Folder konfigurasi global khusus, jika bukan lokasi standar")
    parser.add_argument("--allow-http", action="store_true", help="Izinkan HTTP remote, khusus LAN/VPN tepercaya")
    parser.add_argument("--skip-check", action="store_true", help="Kompatibilitas versi lama: mode manual tanpa cek kini sudah menjadi default")
    parser.add_argument("--context", type=positive_integer, help="Context limit model sebenarnya (token), opsional")
    parser.add_argument("--output", type=positive_integer, help="Output limit model sebenarnya (token), opsional")
    parser.add_argument("--cleanup", action="store_true", help="Batalkan setup lokal milik script tanpa menghapus pengaturan lain")
    parser.add_argument("--dry-run", action="store_true", help="Preview cleanup tanpa mengubah file")
    parser.add_argument("--yes", action="store_true", help="Lewati konfirmasi cleanup (untuk otomatisasi)")
    parser.add_argument("--diagnose", action="store_true", help="Cek koneksi tanpa API key dan tanpa mengubah konfigurasi")
    parser.add_argument("--timeout", type=positive_integer, default=DEFAULT_TIMEOUT, help="Batas tunggu I/O pemeriksaan opsional/diagnostik dalam detik (default: 30)")
    args = parser.parse_args(argv)
    if args.skip_check and (args.list_models or args.check_model):
        parser.error("--skip-check tidak bisa digunakan bersama --list-models atau --check-model")
    if (args.dry_run or args.yes) and not args.cleanup:
        parser.error("--dry-run dan --yes hanya berlaku bersama --cleanup")
    if args.cleanup and (args.url or args.model or args.list_models or args.check_model or args.skip_check or args.context or args.output):
        parser.error("--cleanup tidak digabung dengan opsi setup/model")
    if args.diagnose:
        if args.cleanup or args.model or args.list_models or args.check_model or args.skip_check or args.context or args.output:
            parser.error("--diagnose tidak digabung dengan opsi setup/model/cleanup")
        base_url = normalize_url(args.url or prompt("URL tunnel 9Router"), args.allow_http)
        return diagnose_connection(base_url, timeout=args.timeout)

    path = config_location(args.config_dir)
    if args.cleanup:
        return cleanup(path, dry_run=args.dry_run, yes=args.yes)
    current = parse_jsonc(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    providers = current.get("providers", {})
    provider = providers.get("9router", {}) if isinstance(providers, dict) else {}
    settings = provider.get("settings", {}) if isinstance(provider, dict) else {}
    default_url = settings.get("baseURL", "") if isinstance(settings, dict) else ""
    if not isinstance(default_url, str) or "{" in default_url:
        default_url = ""
    print("Setup OpenCode V2 -> 9Router remote (tidak menjalankan server 9Router lokal).")
    base_url = normalize_url(args.url or prompt("URL tunnel 9Router", default_url), args.allow_http)
    if base_url.startswith("http://"):
        print("PERHATIAN: koneksi HTTP tidak mengenkripsi API key.")
    api_key = os.environ.get("NINEROUTER_API_KEY", "").strip()
    if not api_key:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                api_key = getpass.getpass("API key 9Router (input disembunyikan): ").strip()
            except getpass.GetPassWarning:
                raise SetupError("Input rahasia tidak tersedia. Jalankan di terminal interaktif.") from None
    if not api_key or any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in api_key):
        raise SetupError("API key harus terisi dan tidak boleh mengandung spasi/baris baru/non-ASCII.")

    models = None
    if args.check_model or args.list_models:
        print("Memeriksa koneksi dan API key lewat /v1/models (tanpa request chat)...")
        print(f"Batas tunggu I/O HTTP: {args.timeout} detik; katalog provider 9Router dapat membutuhkan waktu.")
        models = fetch_models(base_url, api_key, timeout=args.timeout)
        print(f"Koneksi OK. Ada {len(models)} model yang terdaftar.")
    else:
        print("Mode manual: tidak mengambil daftar model dan tidak melakukan request jaringan saat setup.")
    if args.list_models:
        for model_id in models:
            print(model_id)
        return 0

    default_model = current.get("model", "")
    default_model = default_model[len("9router/"):] if isinstance(default_model, str) and default_model.startswith("9router/") else ""
    model = args.model or prompt("ID model / nama combo 9Router", default_model)
    if not model or "#" in model or any(char.isspace() or ord(char) < 32 for char in model):
        raise SetupError("ID model harus terisi, tanpa spasi atau #variant. Gunakan ID dari 9Router.")
    if models is not None and model not in models:
        raise SetupError(
            "ID model tidak ada di /models. Lihat --list-models. "
            "Untuk input manual tanpa mengecek katalog, jalankan ulang tanpa --check-model."
        )
    key_path = path.parent / KEY_FILENAME
    config = make_config(current, base_url, model, key_path, args.context, args.output)
    backup = save_config(path, config, api_key, expected=current)
    print(f"\nBerhasil. Konfigurasi global: {path}")
    print(f"Model default: 9router/{model}")
    if backup:
        print(f"Backup konfigurasi sebelumnya: {backup}")
        print("Pengaturan lain dipertahankan; komentar/format JSONC ada di backup, bukan file baru.")
    print(f"API key tersimpan terpisah (plaintext, bukan terenkripsi): {key_path}")
    if os.name == "nt":
        print("Windows: lindungi file key dengan izin NTFS akun sendiri; chmod bukan pembatas ACL.")
    if models is None:
        print("Konfigurasi tersimpan; koneksi, API key, dan model belum diuji. 9Router tetap memvalidasi key saat OpenCode dipakai.")
    print("Cek batas context/output dan dukungan tool model; setup tidak menguji chat/streaming/tool calling.")
    print("Buka opencode. Untuk sesi lama, gunakan /models lalu pilih model 9Router.")
    print("Konfigurasi dimuat ulang otomatis; gunakan /reload bila belum muncul.")
    print("Untuk melepas setup: jalankan script ini lagi dengan --cleanup (preview: --cleanup --dry-run).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SetupError, EOFError, KeyboardInterrupt) as error:
        print(f"\nSetup dibatalkan: {error or 'input dibatalkan'}", file=sys.stderr)
        sys.exit(1)
    except OSError:
        print("\nGagal membaca/menyimpan file. Periksa izin folder konfigurasi.", file=sys.stderr)
        sys.exit(1)
