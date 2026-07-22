# Cloudflare R2 Publisher

Publishes Atlas packages to a Cloudflare R2 bucket.

## Features

- Manifest generation
- SHA-256 calculation
- Incremental uploads
- Automatic MIME type detection
- Retry support
- Upload summary

## Environment

```env
CLOUDFLARE_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=
```

## Usage

```bash
python -m publishers.cloudflare_r2.publish
```