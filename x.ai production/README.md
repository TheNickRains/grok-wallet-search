# GROK Wallet Search

Automated wallet address search on Twitter/X using x.ai's GROK API with x_search tool. Searches for wallet addresses and extracts associated Twitter usernames with confidence scores.

## Features

- 🔍 **Two-Agent Workflow**: First checks if posts exist, then analyzes ownership
- ⚡ **Parallel Processing**: Process multiple wallets concurrently (5x faster)
- 📊 **Multi-Worksheet Support**: Process "Gigabud Holders" and "Grass Claims" worksheets
- 🛡️ **Smart Rate Limiting**: Exponential backoff and request tracking
- 💾 **Checkpoint System**: Resume from interruptions
- 📈 **Google Sheets Integration**: Direct updates to your spreadsheet

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your API keys and credentials
   ```

3. **Run the script:**
   ```bash
   python3 grok_wallet_search.py
   ```

## Configuration

See `.env.example` for all available configuration options.

### Key Environment Variables

- `xai_key` - Your x.ai API key (required)
- `GOOGLE_CREDENTIALS_FILE` - Path to Google Sheets credentials JSON (required)
- `GOOGLE_SHEET_ID` - Your Google Sheet ID (required)
- `WORKSHEETS_TO_PROCESS` - Comma-separated worksheet names (optional)
- `MAX_CONCURRENT_REQUESTS` - Parallel processing limit (default: 5)

## Output

The script updates Google Sheets with:
- **Post Exist?** (Column 5): `"true"` or `"false"`
- **Twitter Handle** (Column 3): `@username` or empty
- **Confidence Score** (Column 6): `"High"`, `"Medium"`, `"Low"`, or `"None"`
- **Script Run** (Column 8): `"true"` when processed

## Performance

- **Sequential**: ~9 seconds per wallet (~3.5 days for 33k wallets)
- **Parallel (5x)**: ~1.8 seconds per wallet (~16.5 hours for 33k wallets)
- **5x speedup** with parallel processing enabled

## Deployment

See [RAILWAY_SETUP.md](./RAILWAY_SETUP.md) for Railway deployment instructions.

## License

MIT

