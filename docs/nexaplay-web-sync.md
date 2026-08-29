# NexaPlay Web Catalog Sync

Bundle ini disalin ke repository `adii83/Nexaplay-Metadata-Override`. Panduan ini ditempatkan sebagai `docs/nexaplay-web-sync.md` agar tidak menimpa README repository tujuan. Workflow membaca sumber metadata, R2, dan katalog website; workflow tidak mengubah `new_games.json`, `nexaplay_override.json`, atau objek R2.

## File yang disalin

Dari root `Nexaplay-Web`:

```bash
mkdir -p ../Nexaplay-Metadata-Override/scripts
mkdir -p ../Nexaplay-Metadata-Override/.github/workflows
mkdir -p ../Nexaplay-Metadata-Override/docs
cp integrations/nexaplay-metadata-override/scripts/sync_nexaplay_web.py ../Nexaplay-Metadata-Override/scripts/
cp integrations/nexaplay-metadata-override/.github/workflows/sync-nexaplay-web.yml ../Nexaplay-Metadata-Override/.github/workflows/
cp integrations/nexaplay-metadata-override/README.md ../Nexaplay-Metadata-Override/docs/nexaplay-web-sync.md
```

Dokumentasi hasil salinan berada di `docs/nexaplay-web-sync.md`; README utama repository tujuan tetap utuh.

## GitHub Actions secrets

Tambahkan lima repository secrets berikut ke `adii83/Nexaplay-Metadata-Override`:

- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`
- `NEXAPLAY_WEB_TOKEN`

Buat R2 API token read-only untuk bucket metadata. Izin minimum:

- `Object Read`
- `Bucket List`

Token tidak boleh punya izin write atau delete.

Buat `NEXAPLAY_WEB_TOKEN` sebagai fine-grained personal access token:

- Repository access: hanya `adii83/Nexaplay-Web`
- Repository permission: `Contents: Read and write`

Workflow repository sumber sendiri memakai `permissions: contents: read`. Jangan simpan nilai secret di workflow, dokumentasi, log, atau output generator.

## R2 CORS

Terapkan konfigurasi CORS berikut ke bucket metadata:

```json
[
  {
    "AllowedOrigins": ["https://nexaplayid.store"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedHeaders": ["*"],
    "ExposeHeaders": ["ETag"],
    "MaxAgeSeconds": 3600
  }
]
```

Verifikasi request browser dari `https://nexaplayid.store` sebelum menghapus fallback lama.

## Menjalankan sinkronisasi

Workflow berjalan saat `new_games.json` atau `nexaplay_override.json` berubah di `main`, setiap hari pukul 02:17 UTC, dan secara manual. Manual run memakai `dry_run: true` secara default. Dry run tetap melakukan fetch dan validasi, tetapi tidak menulis file website, commit, atau push.

Setelah checkout kedua repository secara lokal dan membuat listing R2:

```bash
aws s3api list-objects-v2 \
  --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
  --bucket "$R2_BUCKET_NAME" \
  --prefix Metadata/ \
  --output json > /tmp/r2-metadata.json

python scripts/sync_nexaplay_web.py \
  --source-root . \
  --web-root ../Nexaplay-Web \
  --r2-listing /tmp/r2-metadata.json \
  --metadata-base-url https://meta.nexaplaymetadata.online/Metadata \
  --dry-run
```

Untuk run produksi pertama, jalankan workflow manual dengan `dry_run: false` setelah dry run lulus.

## Baseline pertama

Jika manifest belum ada, sinkronisasi pertama mencatat ETag dan `LastModified` semua objek `Metadata/*.json` tanpa mengunduh seluruh bucket. Metadata hanya diunduh untuk AppID baru dari `new_games.json` dan AppID katalog yang punya override aktif. Run berikutnya mengunduh objek baru atau berubah saja.

Menghapus AppID dari `new_games.json` tidak menghapus game website. Objek R2 juga tidak otomatis memasukkan game baru.

## Push dan pemulihan

Workflow hanya men-stage:

- `web_catalog_builder/games_list.json`
- `web_catalog_builder/output/search_index.json`
- `web_catalog_builder/output/overrides.json`
- `web_catalog_builder/output/r2_manifest.json`

Workflow tidak membuat commit jika staged diff kosong. Jika ada perubahan, workflow membuat commit, menjalankan `git pull --rebase origin main`, lalu plain `git push origin HEAD:main`. Force push dan hard reset dilarang.

Jika pull/rebase atau push gagal karena perubahan upstream/non-fast-forward:

1. Biarkan workflow gagal; jangan force push.
2. Periksa commit terbaru di `adii83/Nexaplay-Web` dan selesaikan konflik pada branch terpisah bila perlu.
3. Jalankan ulang workflow manual dalam dry-run mode.
4. Setelah validasi lulus, jalankan ulang dengan `dry_run: false`.

## Rollback

Pertahankan `web_catalog_builder/output/catalog.json` dan `web_catalog_builder/output/chunks/` selama verifikasi produksi. Jika live R2 detail atau CORS bermasalah, pulihkan frontend ke detail chunk lama. Hapus aset dan jalur rollback itu hanya setelah card, filter, modal detail, override, caching, dan CORS terverifikasi di produksi.
