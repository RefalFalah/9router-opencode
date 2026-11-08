# OpenCode -> 9Router di server lain

Setup sekali pada setiap komputer/server tempat **OpenCode berjalan**. Setelah itu cukup jalankan `opencode` seperti biasa. Tidak perlu menjalankan 9Router kedua atau login ulang ke provider upstream di komputer tujuan.

```text
OpenCode di PC / VPS lain
        |
        | HTTPS + API key 9Router
        v
URL tunnel -> server 9Router Anda -> provider/model Anda
```

## Persiapan

- Python **3.9+** dan OpenCode **V2** sudah terpasang di komputer tujuan.
- Server asal tetap menjalankan 9Router beserta tunnel-nya.
- URL tunnel HTTPS yang bisa diakses dari komputer tujuan.
- API key dari **9Router**, bukan key provider upstream atau password dashboard.
- ID model / nama combo persis dari 9Router, termasuk prefix bila ada, misalnya `kr/claude-sonnet-4.5`. Contoh ini hanya ilustrasi; gunakan model yang tersedia di server Anda.

Script memakai konfigurasi V2: `providers`, `package`, dan `settings`. Script tidak menginstal atau memperbarui OpenCode/9Router.

## Install langsung dari GitHub (tanpa SFTP)

Repository: **https://github.com/RefalFalah/9router-opencode**. Jalankan di komputer tujuan, tanpa `sudo` jika OpenCode juga berjalan dengan akun biasa. Tidak ada paket Python tambahan.

**Windows:**

```powershell
Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/RefalFalah/9router-opencode/main/setup_9router.py' -OutFile setup_9router.py
python .\setup_9router.py
```

**Linux / macOS:**

```bash
curl -fL https://raw.githubusercontent.com/RefalFalah/9router-opencode/main/setup_9router.py -o setup_9router.py
python3 ./setup_9router.py
```

Jalankan perintah Python **hanya jika download berhasil**. Baca kode yang diunduh sebelum mengeksekusinya. Jangan `curl | python`, karena setup memerlukan input terminal. URL `main` mengikuti versi terbaru; untuk versi tetap, ganti `main` dengan commit SHA yang Anda percaya. File script hasil download boleh dihapus setelah setup; unduh lagi jika ingin cleanup. Script tidak bergantung pada folder download.

Kalau sudah punya file script, langsung jalankan perintah Python saja.

Isi tiga pertanyaan:

```text
URL tunnel 9Router: https://router.example.com
API key 9Router (input disembunyikan):
ID model / nama combo 9Router: ID-MODEL-ANDA
```

URL boleh dengan atau tanpa `/v1`; script menambahkan `/v1` jika belum ada. Path prefix seperti `https://example.com/ai/v1` juga didukung. Jangan masukkan URL halaman `/dashboard`.

Setelah berhasil:

```bash
opencode
```

Model 9Router menjadi default untuk pekerjaan baru. Untuk sesi yang sudah punya pilihan model, ketik `/models` dan pilih `9router/ID-MODEL-ANDA`. Konfigurasi dimuat ulang otomatis; `/reload` dapat dipakai jika diperlukan, tidak harus restart service.

## Cleanup / uninstall lokal

Lihat rencana dahulu, **tanpa mengubah file dan tanpa koneksi ke 9Router**:

```bash
python3 setup_9router.py --cleanup --dry-run
```

Untuk benar-benar melepas setup:

```bash
python3 setup_9router.py --cleanup
```

Ketik **`CLEANUP`** saat diminta. Di Windows gunakan `python` sebagai pengganti `python3`. Jika setup memakai `--config-dir`, pakai folder yang sama saat cleanup. Untuk otomatisasi tersedia `--cleanup --yes`; hindari opsi ini jika belum membaca preview.

Mekanismenya:

1. Script menyimpan **state kepemilikan** saat setup: kondisi provider/default sebelumnya, hasil setup terakhir, serta checksum file key.
2. Cleanup hanya membatalkan perubahan provider `9router` milik script. Kalau provider itu sudah ada sebelum setup, **konfigurasi provider sebelumnya dipulihkan**, bukan dihapus seluruhnya.
3. Model default dikembalikan ke kondisi sebelum setup **hanya jika masih sama dengan default terakhir buatan script**. Pilihan default baru yang Anda atur sendiri tidak ditimpa.
4. File key dihapus jika checksum-nya masih cocok dan **tidak dipakai lagi oleh konfigurasi global**. Jika masih dipakai provider lain, key dan state dipertahankan; cleanup dapat diulang setelah referensinya dilepas.
5. State dihapus setelah cleanup tuntas. Mengulang cleanup setelah selesai aman dan tidak menghapus apa pun lagi.

**Yang tidak disentuh:** provider lain, agent, MCP, perubahan konfigurasi lain setelah setup, sesi/riwayat OpenCode, backup, instalasi OpenCode, 9Router di server asal, tunnel, dan kredensial di server. File konfigurasi global tidak dihapus; `$schema` dapat tetap ada.

Jika provider 9Router atau file key diubah di luar script, cleanup/setup berikutnya **berhenti tanpa perubahan** agar tidak merusak konfigurasi manual. Bandingkan dengan state/backup dan putuskan perubahan yang ingin dipertahankan secara manual; tidak ada `--force` yang mengabaikan perlindungan ini. Cleanup tidak menimpa seluruh konfigurasi memakai backup lama, sehingga pengaturan baru di luar 9Router tetap aman.

Cleanup juga membuat backup konfigurasi saat perlu mengubahnya. Backup **tidak ikut dihapus**, dan mungkin masih memuat kredensial lama. Hapus backup sensitif hanya setelah Anda memeriksanya sendiri. Penghapusan file key adalah penghapusan file biasa, **bukan secure erase** dan tidak menjamin salinan di backup filesystem hilang. Untuk mencabut akses sungguhan, **revoke key di dashboard 9Router**; gunakan key per perangkat agar tidak mengganggu komputer lain.

Pemeriksaan referensi key dibatasi pada file global yang dikelola; script tidak menyisir semua project/config lain. Jangan memakai file key yang dikelola script untuk aplikasi/project lain tanpa mengelola dependensinya sendiri. Sesi OpenCode yang sudah berjalan mungkin masih menyimpan kredensial dalam memori; hentikan proses yang memakai key itu dan revoke key bila dibutuhkan.

### Jika pernah memakai script versi awal tanpa state

File key lama tanpa `9router-setup-state.json` **tidak diambil alih atau dihapus otomatis**, karena script tidak bisa memastikan kepemilikannya atau model default awalnya. Cleanup hanya melaporkan bahwa tidak ada state yang dikelola. Untuk migrasi:

1. Periksa backup versi lama dan batalkan perubahan 9Router secara manual; jangan menimpa konfigurasi baru dengan backup penuh tanpa membandingkannya.
2. Setelah memastikan file `9router-api-key` tidak dipakai lagi, hapus file tersebut secara manual. Jika ragu, pertahankan dan cabut key melalui dashboard.
3. Jalankan setup versi ini untuk membuat state kepemilikan yang baru.

## URL dan model lewat argumen

API key tetap ditanyakan secara tersembunyi, bukan dijadikan argumen shell:

```bash
python3 setup_9router.py --url https://router.example.com --model ID-MODEL-ANDA
```

Di Windows, ganti `python3` dengan `python` bila perlu. Pada setup berikutnya, URL dan model konfigurasi sebelumnya ditawarkan sebagai default. Masukkan kembali key ketika diminta; file key tidak ditampilkan.

Untuk melihat ID model yang tersedia, tanpa mengubah konfigurasi:

```bash
python3 setup_9router.py --url https://router.example.com --list-models
```

Untuk otomatisasi, script juga membaca `NINEROUTER_API_KEY` dari environment. Pasok melalui secret manager/environment yang aman; jangan tempel nilai key dalam perintah yang tersimpan di shell history.

### Batas token

Daftar `/models` umumnya tidak memberi tahu batas context/output atau kemampuan tool model. Jika tahu batas sebenarnya, tentukan dalam token:

```bash
python3 setup_9router.py --url https://router.example.com --model ID-MODEL-ANDA --context 32768 --output 8192
```

Angka di atas **contoh**, bukan batas semua model. Bila tidak diberikan, script mempertahankan limit yang sudah dikonfigurasi; untuk model baru OpenCode memakai asumsi default-nya. Sesuaikan sebelum sesi panjang. Gunakan model dan endpoint yang mendukung **tool calling** agar OpenCode dapat memakai tools. Script hanya memeriksa `/models`, **bukan** membuktikan chat, streaming, atau tool calling berhasil, dan tidak melakukan request chat berbayar.

## File yang disimpan

Lokasi default:

- Windows: `%USERPROFILE%\.config\opencode\`
- Linux/macOS: `~/.config/opencode/`
- Jika `XDG_CONFIG_HOME` terisi: `$XDG_CONFIG_HOME/opencode/`

Jika OpenCode memakai lokasi berbeda, cek `opencode debug paths config`, lalu gunakan `--config-dir PATH-YANG-DITAMPILKAN`.

Script:

1. Membaca `opencode.json` atau `opencode.jsonc` yang sudah ada. Jika keduanya ada, script berhenti agar tidak salah mengedit file.
2. Mengecek URL, API key, dan ID model melalui `GET /v1/models`.
3. Menyalin konfigurasi lama ke `opencode.json(c).bak-9router-TIMESTAMP`.
4. Menambahkan/memperbarui hanya provider `9router`, model yang dipilih, dan model default; pengaturan lainnya tetap ada. Model 9Router lain tetap dipertahankan.
5. Menyimpan API key ke file **`9router-api-key`**, lalu menggunakan substitusi `{file:...}` di konfigurasi. Key tidak dimasukkan langsung ke JSON.
6. Menyimpan **`9router-setup-state.json`** untuk cleanup. State tidak berisi key baru dalam plaintext, tetapi snapshot konfigurasi lama **bisa mengandung kredensial lama** bila sebelumnya disimpan inline.

JSONC dengan komentar dan trailing comma bisa dibaca. File hasil diformat ulang menjadi JSON; **komentar dan format asli hanya ada dalam backup**. Backup juga mungkin mengandung rahasia yang sudah ada di konfigurasi lama.

API key tetap **plaintext di disk**, bukan terenkripsi. File baru/state/backup dibuat dengan mode `0600` di POSIX. Di Windows, mode ini **bukan** pengaturan ACL: pastikan izin NTFS file/folder membatasi akses ke akun Anda dan administrator yang Anda percaya. Jangan upload key, state, backup, atau folder konfigurasi ke Git, chat, maupun repositori publik. Repository script menyertakan `.gitignore` untuk membantu mencegahnya; tetap periksa daftar file sebelum commit.

Backup ini hanya mencadangkan **konfigurasi**, bukan versi API key sebelumnya. Setup berikutnya mengganti file key yang sama. Jika memulihkan konfigurasi lama setelah ganti endpoint/key, masukkan kembali key yang sesuai.

Script tidak mengubah konfigurasi global komputer ini sampai benar-benar Anda jalankan. Menjalankan tes hanya menggunakan folder sementara dan server HTTP lokal buatan.

## Kalau belum berhasil

| Gejala | Yang diperiksa |
| --- | --- |
| `401` | Gunakan API key 9Router yang masih aktif. |
| `403` / respons HTML / redirect login | Periksa izin key, proteksi tunnel, atau Cloudflare Access. Proteksi tambahan bisa memerlukan service token/header tersendiri; script ini hanya memakai Bearer API key. |
| `404` | URL harus mengarah ke API 9Router; periksa `/v1` dan path prefix reverse proxy. |
| Tidak bisa terhubung | 9Router/tunnel harus tetap hidup; periksa DNS, firewall, dan sertifikat HTTPS. |
| Model tidak terdaftar | Lihat `--list-models`, gunakan ID persis termasuk prefix provider. Untuk combo yang benar-benar ada tetapi tidak diiklankan, tersedia `--skip-check`; hasilnya belum terverifikasi. |
| Model lain tetap terpilih | Pilih lewat `/models`; model sesi, agent, konfigurasi project, atau kebijakan managed bisa mengalahkan default global. |
| Tool/streaming gagal meski setup sukses | Cek dukungan tool calling model dan SSE/timeouts di tunnel. Daftar model berhasil tidak menjamin seluruh fitur API kompatibel. |

HTTP remote ditolak secara default. Hanya untuk LAN/VPN tepercaya, tersedia `--allow-http`; API key tetap tidak terenkripsi oleh HTTP. Jangan mematikan verifikasi TLS untuk mengatasi masalah sertifikat.

Jika memakai `opencode --server ...`, konfigurasi provider dibaca di **server OpenCode tersebut**, bukan otomatis dari komputer TUI. Jalankan setup pada mesin/akun server yang melakukan request model.

**Keamanan server:** wajibkan autentikasi API di 9Router dan lindungi dashboard. Memasukkan key di client tidak otomatis mengamankan endpoint yang di server masih terbuka. Gunakan HTTPS dan, bila tersedia, key terpisah per perangkat agar mudah dicabut. URL tunnel sementara bisa berubah; jalankan setup lagi ketika URL berubah. Kuota/rate limit tetap mengikuti akun provider Anda dan dipakai bersama semua perangkat.

## Tes

```bash
python3 -m unittest discover -s tests -v
```

Referensi:

- [OpenCode V2: custom providers](https://opencode.ai/docs/providers#custom)
- [OpenCode: global config dan substitusi file](https://opencode.ai/docs/config)
- [9Router: API OpenAI-compatible](https://github.com/decolua/9router/blob/master/README.md)
