# Railway Deployment Guide

This guide explains how to deploy the GROK wallet search script to Railway for batch processing.

## Prerequisites

1. Railway account at [railway.app](https://railway.app)
2. x.ai API key from [console.x.ai](https://console.x.ai)
3. Google Sheets credentials JSON file
4. Google Sheet ID with worksheets:
   - "Gigabud Holders" (~3k rows)
   - "Grass Claims" (~30k rows)

## Setup Steps

### 1. Create Railway Project

1. Sign up/login at railway.app
2. Create a new project
3. Select "Empty Project" or "Python" template

### 2. Connect Repository

- **Option A: GitHub Integration (Recommended)**
  - Push your code to GitHub
  - In Railway dashboard, click "New" → "GitHub Repo"
  - Select your repository

- **Option B: Direct Upload**
  - Use Railway CLI: `railway init`
  - Upload files via Railway dashboard

### 3. Configure Environment Variables

In Railway dashboard, go to your service → Variables tab, add:

**Required:**
```
xai_key=your_xai_api_key_here
GOOGLE_CREDENTIALS_FILE=/path/to/credentials.json
GOOGLE_SHEET_ID=your_sheet_id
```

**Optional (with defaults):**
```
GROK_MODEL=grok-4-fast
WALLET_LIMIT=0
RATE_LIMIT_DELAY=1
RATE_LIMIT_ERROR_DELAY=60
RAILWAY_VOLUME_MOUNT_PATH=/tmp
MAX_CONCURRENT_REQUESTS=5
RATE_LIMIT_WINDOW=60
MAX_REQUESTS_PER_WINDOW=50
USE_PARALLEL=true
WORKSHEET_NAME=Gigabud Holders
WORKSHEETS_TO_PROCESS=Gigabud Holders,Grass Claims
```

### 4. Upload Google Credentials

**Option A: Environment Variable (for small files)**
- Convert credentials JSON to base64: `cat credentials.json | base64`
- Add as environment variable: `GOOGLE_CREDENTIALS_BASE64=...`
- Script will decode automatically

**Option B: Railway Volume (Recommended)**
1. Create a volume in Railway dashboard
2. Mount it to `/data`
3. Upload `credentials.json` to the volume
4. Set `GOOGLE_CREDENTIALS_FILE=/data/credentials.json`

### 5. Configure Service Type

**For One-Off Batch Job:**
- Service Type: "Worker"
- Start Command: `python3 grok_wallet_search.py`
- The service will exit when done

**For Scheduled Jobs:**
- Use Railway Cron Jobs feature
- Schedule: `0 0 * * *` (daily at midnight) or custom schedule

### 6. Deploy

1. Click "Deploy" in Railway dashboard
2. Monitor logs in real-time
3. Check Google Sheets for results

## Configuration Options

### Processing Limits

- `WALLET_LIMIT=5` - Process first 5 wallets (testing)
- `WALLET_LIMIT=0` - Process all wallets (full run) - **Default**
- `WALLET_LIMIT=100` - Process 100 wallets

### Multi-Worksheet Support

- `WORKSHEET_NAME=Gigabud Holders` - Process single worksheet (default: "Gigabud Holders")
- `WORKSHEETS_TO_PROCESS=Gigabud Holders,Grass Claims` - Process multiple worksheets (comma-separated)
- Each worksheet has its own checkpoint file (e.g., `grok_checkpoint_gigabud_holders.txt`)

### Parallel Processing

- `USE_PARALLEL=true` - Enable parallel processing (default: true)
- `MAX_CONCURRENT_REQUESTS=5` - Max concurrent API requests (default: 5, adjust based on rate limits)
- **Performance**: ~5x faster with parallel processing enabled

### Rate Limiting

- `RATE_LIMIT_DELAY=1` - Wait 1 second between wallets (reduced from 3)
- `RATE_LIMIT_ERROR_DELAY=60` - Base delay on rate limit errors (exponential backoff applied)
- `RATE_LIMIT_WINDOW=60` - Time window for rate limit tracking (seconds)
- `MAX_REQUESTS_PER_WINDOW=50` - Max requests per time window (adjust based on API tier)
- **Features**: Exponential backoff, request tracking, proactive rate limit prevention

### Model Selection

- `GROK_MODEL=grok-4-fast` - Faster, cheaper (default)
- `GROK_MODEL=grok-4` - Better reasoning, slower

## Monitoring

- **Logs**: View real-time logs in Railway dashboard
- **Checkpoints**: Checkpoint file saved for resuming
- **Google Sheets**: Results updated in real-time

## Cost Estimates

### Railway Costs
- **Free Tier**: $5/month credit (enough for testing)
- **Compute**: ~$0.04/vCPU-hour + $0.08/GB RAM-hour
- **Full 33k run**: ~$1-5 (~16.5 hours runtime with parallel processing)

### x.ai API Costs
- **Input**: ~$5 per 1M tokens
- **Output**: ~$15 per 1M tokens
- **Full 33k run**: ~$10-40 (depending on hit rate)

### Total Estimate
- **Testing (5 wallets)**: < $0.10
- **Full run (33k wallets)**: ~$11-45

## Troubleshooting

### Rate Limit Errors
- Script uses exponential backoff (60s, 120s, 240s, capped at 5 minutes)
- Automatically tracks requests per time window to prevent hitting limits
- Increase `MAX_REQUESTS_PER_WINDOW` if you have higher tier access
- Reduce `MAX_CONCURRENT_REQUESTS` if hitting rate limits frequently
- Check for `RESOURCE_EXHAUSTED` gRPC errors in logs

### Checkpoint Issues
- Checkpoint files are worksheet-specific: `grok_checkpoint_{worksheet_name}.txt`
- Default location: `/tmp/grok_checkpoint_gigabud_holders.txt`
- For persistent storage, use Railway volume: `/data/grok_checkpoint_{worksheet_name}.txt`
- Each worksheet maintains its own checkpoint for independent resuming

### Google Sheets Errors
- Verify credentials file path is correct
- Check sheet ID and worksheet name (supports "Gigabud Holders" and "Grass Claims")
- Ensure service account has edit permissions
- Verify worksheet names match exactly (case-sensitive)

### Parallel Processing Issues
- If hitting rate limits, reduce `MAX_CONCURRENT_REQUESTS` (try 3 instead of 5)
- Disable parallel processing: `USE_PARALLEL=false` for sequential processing
- Monitor logs for concurrent request patterns

### Memory Issues
- Railway free tier: 512MB RAM
- For large runs, upgrade to Pro plan ($20/month)

## Resuming Interrupted Jobs

The script automatically saves checkpoints per worksheet. To resume:

1. Run the script again (it will detect worksheet-specific checkpoint)
2. Each worksheet resumes from its own checkpoint independently
3. Set `WALLET_LIMIT=0` to process all remaining wallets from checkpoint

## Output Columns

The script updates Google Sheets with the following columns:
- **Post Exist?** (Column 5): `"true"` or `"false"`
- **Twitter Handle** (Column 3): `@username` or empty
- **Confidence Score** (Column 6): `"High"`, `"Medium"`, `"Low"`, or `"None"` (case-sensitive)
- **Script Run** (Column 8): `"true"` when wallet has been processed

## Best Practices

1. **Test First**: Run with `WALLET_LIMIT=5` before full run
2. **Monitor Logs**: Watch for errors and rate limit warnings in Railway dashboard
3. **Check Results**: Verify Google Sheets updates periodically
4. **Parallel Processing**: Start with `MAX_CONCURRENT_REQUESTS=5`, reduce if hitting rate limits
5. **Multi-Worksheet**: Process worksheets sequentially for better control, or in parallel if needed
6. **Rate Limits**: Script automatically handles with exponential backoff, but monitor for patterns
7. **Off-Peak**: Run during off-peak hours to avoid API queues
8. **Performance**: With parallel processing, expect ~5x speedup (16.5 hours for 33k wallets vs 3.5 days sequential)

## Example Configurations

### Process Single Worksheet (Gigabud Holders)
```
WORKSHEET_NAME=Gigabud Holders
WALLET_LIMIT=0
MAX_CONCURRENT_REQUESTS=5
```

### Process Multiple Worksheets
```
WORKSHEETS_TO_PROCESS=Gigabud Holders,Grass Claims
WALLET_LIMIT=0
MAX_CONCURRENT_REQUESTS=5
```

### Conservative Rate Limiting
```
MAX_CONCURRENT_REQUESTS=3
MAX_REQUESTS_PER_WINDOW=30
RATE_LIMIT_DELAY=2
```

### Maximum Performance
```
MAX_CONCURRENT_REQUESTS=10
MAX_REQUESTS_PER_WINDOW=100
RATE_LIMIT_DELAY=0.5
```
