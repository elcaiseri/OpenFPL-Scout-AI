# Static Files Configuration

This document explains the static files serving configuration added to optimize GCP storage costs.

## Overview

The application now supports multiple strategies for serving static files:
- **Local**: Traditional mounting of static directories (development)
- **GCS**: Serving from Google Cloud Storage bucket (production)
- **CDN**: Serving from Content Delivery Network (production)

## Configuration

### Environment Variables

- `ENVIRONMENT`: Set to `development` or `production` (default: `development`)
- `STATIC_FILES_STRATEGY`: Set to `local`, `gcs`, or `cdn` (default: `local`)

### Config File (`config/config.yaml`)

```yaml
# Environment Configuration
environment:
  type: "development"  # development or production

# Static File Configuration
static_files:
  strategy: "local"  # local, gcs, or cdn
  
  # GCS Configuration (used when strategy is 'gcs')
  gcs:
    bucket_name: "your-bucket-name"
    base_url: ""  # optional, auto-generated if empty
  
  # CDN Configuration (used when strategy is 'cdn')
  cdn:
    base_url: "https://your-cdn-domain.com"
  
  # Local static directories (used when strategy is 'local')
  local:
    directories:
      - name: "static"
        path: "static"
        mount_path: "/static"
      - name: "assets" 
        path: "assets"
        mount_path: "/assets"
      - name: "data"
        path: "data"
        mount_path: "/data"
```

## Behavior

### Development Mode (`ENVIRONMENT=development`)
- Static files are always mounted and served locally
- Ignores the static files strategy setting
- Used for local development and testing

### Production Mode (`ENVIRONMENT=production`)
- Static files behavior depends on the strategy:
  - `local`: Mounts static files (same as development)
  - `gcs`: No mounting, expects files to be served from GCS bucket
  - `cdn`: No mounting, expects files to be served from CDN

## Usage Examples

### Local Development
```bash
# Default behavior - mounts all static files
python -m uvicorn main:app --reload
```

### Production with GCS
```bash
ENVIRONMENT=production STATIC_FILES_STRATEGY=gcs python -m uvicorn main:app
```

### Production with CDN
```bash
ENVIRONMENT=production STATIC_FILES_STRATEGY=cdn python -m uvicorn main:app
```

## Cost Optimization

This configuration helps reduce GCP storage costs by:

1. **Development**: Full static file support for local development
2. **Production**: Eliminates static file mounting, reducing container size and storage costs
3. **Flexibility**: Easy switching between local, GCS, and CDN strategies

## Migration Guide

### From Previous Version
1. Update your deployment configuration to set `ENVIRONMENT=production`
2. Set `STATIC_FILES_STRATEGY=gcs` for GCS bucket serving
3. Configure your GCS bucket name in `config.yaml`
4. Upload your static files to the GCS bucket
5. Update HTML templates if needed to use absolute URLs

### Future GCS Integration
The `get_static_file_url()` utility function is provided for generating appropriate URLs based on the current strategy. This can be used in templates or API responses.