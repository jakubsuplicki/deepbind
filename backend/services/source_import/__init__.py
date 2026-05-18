"""Folder/source import services.

Step 29a starts with trusted source grants plus metadata-only scans. Step 29b
adds review selections, and Step 29c starts reusable business document
extraction plus approved local-folder import batches. Step 29d adds import
lifecycle cleanup with completed-batch removal, cancellation, interrupted
batch recovery, skipped/failed review, explicit metadata-only
rescan/import-changes, buyer-demo completion summaries, sample-data grants,
conservative cloud-placeholder handling, cross-batch content dedupe, and first
ZIP child scanning/import for folder-contained and standalone archive sources.
Archive guards reject encrypted members before extraction.
"""
