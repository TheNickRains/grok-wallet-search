#!/usr/bin/env python3
"""
GROK-powered wallet search using x.ai SDK with x_search tool
Searches Twitter/X for wallet addresses and extracts usernames with confidence scores
"""

import os
import re
import asyncio
import time
import logging
import json
import tempfile
from datetime import datetime
from collections import deque
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from xai_sdk import Client
from xai_sdk.chat import user
from xai_sdk.tools import x_search

# Try to import grpc for better error handling
try:
    import grpc
    GRPC_AVAILABLE = True
except ImportError:
    GRPC_AVAILABLE = False

# Load environment variables
load_dotenv()

# Setup logging for Railway console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class GrokWalletSearcher:
    def __init__(self, worksheet_name=None, shared_semaphore=None):
        """Initialize GROK client and Google Sheets connection
        
        Args:
            worksheet_name: Name of the worksheet to process
            shared_semaphore: Optional shared semaphore for cross-worksheet concurrency control
        """
        # Initialize x.ai client
        api_key = os.environ.get("xai_key") or os.environ.get("XAI_API_KEY")
        if not api_key:
            raise ValueError("xai_key or XAI_API_KEY not found in environment. Please add it to .env file.")
        
        self.client = Client(api_key=api_key)
        self.model = os.environ.get("GROK_MODEL", "grok-4-fast")  # Use fast model for speed/cost efficiency
        
        # Worksheet name
        self.worksheet_name = worksheet_name or os.environ.get("WORKSHEET_NAME", "Gigabud Holders")
        
        # Parallel processing configuration (initialize before setup_google_sheets for logging)
        self.max_concurrent = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "5"))  # Max concurrent requests
        # Use shared semaphore if provided (for cross-worksheet concurrency control), otherwise create new one
        self.semaphore = shared_semaphore if shared_semaphore is not None else asyncio.Semaphore(self.max_concurrent)
        
        # Initialize Google Sheets
        self.setup_google_sheets()
        
        # Checkpoint file (use Railway volume path if available)
        checkpoint_dir = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "/tmp")
        # Create checkpoint filename based on worksheet name
        checkpoint_suffix = self.worksheet_name.lower().replace(" ", "_")
        self.checkpoint_file = os.path.join(checkpoint_dir, f"grok_checkpoint_{checkpoint_suffix}.txt")
        
        # Rate limit configuration
        self.rate_limit_delay = int(os.environ.get("RATE_LIMIT_DELAY", "1"))  # Reduced default delay
        self.rate_limit_error_delay = int(os.environ.get("RATE_LIMIT_ERROR_DELAY", "60"))  # Seconds on rate limit error
        
        # Rate limiting tracking
        self.request_times = deque()  # Track request timestamps
        self.rate_limit_window = int(os.environ.get("RATE_LIMIT_WINDOW", "60"))  # 60 second window
        self.max_requests_per_window = int(os.environ.get("MAX_REQUESTS_PER_WINDOW", "50"))  # Adjust based on tier
        self.consecutive_rate_limits = 0  # Track consecutive rate limit errors
        
    def setup_google_sheets(self):
        """Setup Google Sheets client"""
        sheet_id = os.environ.get("GOOGLE_SHEET_ID")
        
        if not sheet_id:
            raise ValueError("GOOGLE_SHEET_ID required in environment")
        
        scope = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        
        # Check for JSON credentials in environment variable first
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE")
        
        if creds_json:
            # Parse JSON from environment variable
            try:
                # Strip whitespace and try to extract JSON if there's extra content
                creds_json = creds_json.strip()
                
                # Try to find JSON object if there's extra text
                if not creds_json.startswith('{'):
                    # Look for first { and last }
                    start_idx = creds_json.find('{')
                    end_idx = creds_json.rfind('}')
                    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                        creds_json = creds_json[start_idx:end_idx + 1]
                        logger.info("⚠️  Extracted JSON from GOOGLE_CREDENTIALS_JSON (removed extra content)")
                
                creds_info = json.loads(creds_json)
                creds = Credentials.from_service_account_info(creds_info, scopes=scope)
                logger.info("✅ Using credentials from GOOGLE_CREDENTIALS_JSON environment variable")
            except json.JSONDecodeError as e:
                logger.error(f"❌ JSON parse error at position {e.pos}: {e.msg}")
                logger.error(f"   JSON content preview: {creds_json[:200]}...")
                raise ValueError(f"Invalid JSON in GOOGLE_CREDENTIALS_JSON: {e.msg} at position {e.pos}. Please ensure the JSON is valid and properly formatted.")
        elif creds_file:
            # Use credentials file
            if not os.path.exists(creds_file):
                raise FileNotFoundError(f"Credentials file not found: {creds_file}")
            creds = Credentials.from_service_account_file(creds_file, scopes=scope)
            logger.info(f"✅ Using credentials from file: {creds_file}")
        else:
            raise ValueError("Either GOOGLE_CREDENTIALS_JSON or GOOGLE_CREDENTIALS_FILE must be set in environment")
        
        self.sheets_client = gspread.authorize(creds)
        
        # Open spreadsheet and worksheet
        self.spreadsheet = self.sheets_client.open_by_key(sheet_id)
        self.worksheet = self.spreadsheet.worksheet(self.worksheet_name)
        
        # Find column indices
        headers = self.worksheet.row_values(1)
        self.wallet_col = -1
        self.post_exist_col = -1
        self.twitter_handle_col = -1
        self.confidence_col = -1
        self.script_run_col = -1
        
        for i, header in enumerate(headers):
            header_lower = header.lower()
            if "wallet" in header_lower and "address" in header_lower:
                self.wallet_col = i + 1  # gspread uses 1-based indexing
            elif "post exist" in header_lower or "post_exist" in header_lower:
                self.post_exist_col = i + 1
            elif "twitter" in header_lower and "handle" in header_lower:
                self.twitter_handle_col = i + 1
            elif "confidence" in header_lower and "score" in header_lower:
                self.confidence_col = i + 1
            elif "script" in header_lower and "run" in header_lower:
                self.script_run_col = i + 1
        
        # Add columns if they don't exist
        if self.post_exist_col == -1:
            self.worksheet.insert_cols([["Post Exist?"]], len(headers) + 1)
            self.post_exist_col = len(headers) + 1
        
        if self.twitter_handle_col == -1:
            self.worksheet.insert_cols([["Twitter Handle"]], len(headers) + 2)
            self.twitter_handle_col = len(headers) + 2
        
        if self.confidence_col == -1:
            self.worksheet.insert_cols([["Confidence Score"]], len(headers) + 3)
            self.confidence_col = len(headers) + 3
        
        # Ensure Script Run column is at column 8
        if self.script_run_col == -1:
            # Check if column 8 exists and is empty, or insert new column
            if len(headers) < 8:
                # Need to add columns to reach column 8
                while len(headers) < 7:
                    self.worksheet.insert_cols([[""]], len(headers) + 1)
                    headers.append("")
                self.worksheet.insert_cols([["Script Run"]], 8)
            else:
                # Column 8 exists, check if it's the script run column
                col8_header = headers[7] if len(headers) > 7 else ""
                if not col8_header or "script" not in col8_header.lower():
                    # Update column 8 header
                    self.worksheet.update_cell(1, 8, "Script Run")
            self.script_run_col = 8
        elif self.script_run_col != 8:
            # Script run column exists but not at column 8, update column 8
            self.worksheet.update_cell(1, 8, "Script Run")
            self.script_run_col = 8
        
        logger.info("✅ Google Sheets connected")
        logger.info(f"   Wallet column: {self.wallet_col}")
        logger.info(f"   Post Exist column: {self.post_exist_col}")
        logger.info(f"   Twitter Handle column: {self.twitter_handle_col}")
        logger.info(f"   Confidence Score column: {self.confidence_col}")
        logger.info(f"   Script Run column: {self.script_run_col}")
        logger.info(f"   Worksheet: {self.worksheet_name}")
        logger.info(f"   Max concurrent requests: {self.max_concurrent}")
    
    async def wait_for_rate_limit_window(self):
        """Wait if we're approaching rate limit"""
        now = time.time()
        # Remove requests outside the window
        while self.request_times and self.request_times[0] < now - self.rate_limit_window:
            self.request_times.popleft()
        
        # If we're at the limit, wait
        if len(self.request_times) >= self.max_requests_per_window:
            wait_time = self.rate_limit_window - (now - self.request_times[0])
            if wait_time > 0:
                logger.info(f"   ⏳ Approaching rate limit, waiting {wait_time:.1f} seconds...")
                await asyncio.sleep(wait_time)
        
        # Record this request
        self.request_times.append(time.time())
    
    async def handle_rate_limit_error(self, attempt, max_retries):
        """Handle rate limit with exponential backoff"""
        self.consecutive_rate_limits += 1
        
        # Exponential backoff: 60s, 120s, 240s (capped at 5 minutes)
        base_delay = 60
        backoff_delay = min(base_delay * (2 ** (attempt - 1)), 300)
        
        # If multiple consecutive rate limits, increase delay
        if self.consecutive_rate_limits > 1:
            backoff_delay *= min(self.consecutive_rate_limits, 3)  # Cap multiplier at 3x
        
        logger.warning(f"   ⚠️  Rate limit detected (attempt {attempt}/{max_retries}, consecutive: {self.consecutive_rate_limits})")
        logger.warning(f"   ⏳ Waiting {backoff_delay} seconds (exponential backoff)...")
        await asyncio.sleep(backoff_delay)
    
    def extract_username(self, content):
        """Extract Twitter username from GROK response using regex"""
        # Try multiple patterns
        patterns = [
            r'username[:\s]+@?([A-Za-z0-9_]{1,15})',  # "username: @handle" or "username: handle"
            r'@([A-Za-z0-9_]{1,15})',  # Just @handle
            r'handle[:\s]+@?([A-Za-z0-9_]{1,15})',  # "handle: @username"
            r'twitter[:\s]+@?([A-Za-z0-9_]{1,15})',  # "twitter: @username"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                username = match.group(1)
                # Validate username format (1-15 chars, alphanumeric + underscore)
                if 1 <= len(username) <= 15 and re.match(r'^[A-Za-z0-9_]+$', username):
                    return username
        
        return None
    
    def extract_confidence_level(self, content):
        """Extract confidence level from GROK response (High, Medium, Low, None)"""
        content_lower = content.lower()
        
        # Look for confidence level keywords
        if re.search(r'\b(high|strong|clear|definite|certain)\b', content_lower):
            return "High"
        elif re.search(r'\b(medium|moderate|somewhat|partial)\b', content_lower):
            return "Medium"
        elif re.search(r'\b(low|weak|minimal|uncertain)\b', content_lower):
            return "Low"
        elif re.search(r'\b(none|no|false|not found)\b', content_lower):
            return "None"
        
        # Also check for explicit "Confidence: high/medium/low/none" format
        confidence_patterns = [
            r'confidence[:\s]+(high|medium|low|none)',
            r'confidence[:\s]+(strong|moderate|weak|none)',
            r'level[:\s]+(high|medium|low|none)',
        ]
        
        for pattern in confidence_patterns:
            match = re.search(pattern, content_lower)
            if match:
                level = match.group(1).lower()
                if level in ["high", "strong"]:
                    return "High"
                elif level in ["medium", "moderate"]:
                    return "Medium"
                elif level in ["low", "weak"]:
                    return "Low"
                elif level == "none":
                    return "None"
        
        return None
    
    async def agent_check_post_exists(self, wallet, max_retries=3):
        """Agent 1: Check if any post exists containing the wallet address"""
        # Wait for rate limit window before making request
        await self.wait_for_rate_limit_window()
        
        query = f'Search X for any posts containing the exact phrase "{wallet}". Respond with only "true" if any post exists, or "false" if no posts are found. Do not provide any other information.'
        
        for attempt in range(max_retries):
            try:
                logger.info(f"   Agent 1 - Attempt {attempt + 1}/{max_retries}...")
                
                # Create chat with x_search tool
                chat = self.client.chat.create(model=self.model, tools=[x_search()])
                chat.append(user(query))
                
                # Get response
                response = chat.sample()
                content = response.content.strip().lower()
                
                # Reset consecutive rate limits on success
                self.consecutive_rate_limits = 0
                
                # Check for true/false
                if "true" in content and "false" not in content:
                    logger.info("   ✅ Agent 1: Post exists")
                    return True, content
                elif "false" in content:
                    logger.info("   ✅ Agent 1: No posts found")
                    return False, content
                else:
                    # Ambiguous response, default to false
                    logger.warning(f"   ⚠️  Agent 1: Ambiguous response, defaulting to false")
                    return False, content
                
            except Exception as e:
                error_str = str(e).lower()
                logger.error(f"   ❌ Agent 1 error on attempt {attempt + 1}: {e}")
                
                # Check for gRPC RESOURCE_EXHAUSTED error
                is_rate_limit = False
                if GRPC_AVAILABLE:
                    try:
                        if hasattr(e, 'code') and e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                            is_rate_limit = True
                    except:
                        pass
                
                # Also check string-based detection
                if not is_rate_limit and ("rate limit" in error_str or "429" in error_str or "too many requests" in error_str or "resource_exhausted" in error_str):
                    is_rate_limit = True
                
                if is_rate_limit:
                    await self.handle_rate_limit_error(attempt + 1, max_retries)
                    continue
                
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    logger.info(f"   ⏳ Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    return False, f"Error: {str(e)}"
        
        return False, "Max retries exceeded"
    
    async def agent_analyze_ownership(self, wallet, max_retries=3):
        """Agent 2: Analyze posts to determine wallet ownership and confidence level"""
        # Wait for rate limit window before making request
        await self.wait_for_rate_limit_window()
        
        query = f'''Search X for all posts containing the exact phrase "{wallet}". 

Analyze the context of each post to determine:
1. Who posted it (username/handle)
2. Whether this wallet address belongs to that user (confidence level: high, medium, low, or none)

Confidence level guidelines:
- "High": Clear ownership (user's own post in airdrop thread, wallet sharing, profile bio, explicit ownership statements)
- "Medium": Strong indication (user sharing their wallet for donations, trading, or in context of their activity)
- "Low": Weak indication (user just mentioned or quoted it, minimal context)
- "None": Very weak or no indication of ownership

Return the username and confidence level in this format:
Username: @handle
Confidence: [High|Medium|Low|None]

If multiple posts exist, analyze all of them and provide the highest confidence level with the associated username.'''
        
        for attempt in range(max_retries):
            try:
                logger.info(f"   Agent 2 - Attempt {attempt + 1}/{max_retries}...")
                
                # Create chat with x_search tool
                chat = self.client.chat.create(model=self.model, tools=[x_search()])
                chat.append(user(query))
                
                # Get response
                response = chat.sample()
                content = response.content
                
                # Reset consecutive rate limits on success
                self.consecutive_rate_limits = 0
                
                # Extract username and confidence level
                username = self.extract_username(content)
                confidence_level = self.extract_confidence_level(content)
                
                if username:
                    # If confidence level not found, default to "Medium"
                    final_confidence = confidence_level if confidence_level is not None else "Medium"
                    logger.info(f"   ✅ Agent 2: Username: @{username}, Confidence: {final_confidence}")
                    return {
                        'username': username,
                        'confidence': final_confidence,
                        'raw_response': content
                    }
                else:
                    logger.warning(f"   ⚠️  Agent 2: Could not parse username from response")
                    logger.debug(f"   Raw response: {content[:200]}...")
                    return {
                        'username': None,
                        'confidence': confidence_level if confidence_level is not None else "Medium",
                        'raw_response': content,
                        'error': 'Could not parse username'
                    }
                
            except Exception as e:
                error_str = str(e).lower()
                logger.error(f"   ❌ Agent 2 error on attempt {attempt + 1}: {e}")
                
                # Check for gRPC RESOURCE_EXHAUSTED error
                is_rate_limit = False
                if GRPC_AVAILABLE:
                    try:
                        if hasattr(e, 'code') and e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                            is_rate_limit = True
                    except:
                        pass
                
                # Also check string-based detection
                if not is_rate_limit and ("rate limit" in error_str or "429" in error_str or "too many requests" in error_str or "resource_exhausted" in error_str):
                    is_rate_limit = True
                
                if is_rate_limit:
                    await self.handle_rate_limit_error(attempt + 1, max_retries)
                    continue
                
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    logger.info(f"   ⏳ Waiting {wait_time} seconds before retry...")
                    await asyncio.sleep(wait_time)
                else:
                    return {
                        'username': None,
                        'confidence': None,
                        'raw_response': '',
                        'error': str(e)
                    }
        
        return {
            'username': None,
            'confidence': None,
            'raw_response': '',
            'error': 'Max retries exceeded'
        }
    
    async def check_wallet(self, wallet, max_retries=3):
        """Two-agent workflow: First check if post exists, then analyze ownership"""
        logger.info(f"🔍 Checking wallet: {wallet[:20]}...")
        
        # Agent 1: Check if post exists
        post_exists, agent1_response = await self.agent_check_post_exists(wallet, max_retries)
        
        if not post_exists:
            logger.info("   ✅ No posts found")
            return {
                'status': 'false',
                'username': None,
                'confidence': 'None',
                'raw_response': agent1_response
            }
        
        # Agent 2: Analyze ownership and confidence
        logger.info("   🔍 Post found, analyzing ownership...")
        ownership_result = await self.agent_analyze_ownership(wallet, max_retries)
        
        if ownership_result.get('username'):
            logger.info(f"   ✅ Analysis complete! Username: @{ownership_result['username']}, Confidence: {ownership_result['confidence']}")
            return {
                'status': 'true',
                'username': ownership_result['username'],
                'confidence': ownership_result['confidence'],
                'raw_response': ownership_result.get('raw_response', ''),
                'agent1_response': agent1_response
            }
        else:
            logger.warning(f"   ⚠️  Post exists but ownership analysis failed")
            return {
                'status': 'true',
                'username': None,
                'confidence': ownership_result.get('confidence', 'Low'),
                'raw_response': ownership_result.get('raw_response', ''),
                'agent1_response': agent1_response,
                'error': ownership_result.get('error', 'Could not determine ownership')
            }
    
    def load_checkpoint(self):
        """Load checkpoint to resume from previous run"""
        try:
            # Ensure checkpoint directory exists
            checkpoint_dir = os.path.dirname(self.checkpoint_file)
            if checkpoint_dir and not os.path.exists(checkpoint_dir):
                os.makedirs(checkpoint_dir, exist_ok=True)
            
            with open(self.checkpoint_file, 'r') as f:
                checkpoint = int(f.read().strip())
                logger.info(f"📋 Resuming from checkpoint: row {checkpoint}")
                return checkpoint
        except FileNotFoundError:
            logger.info("📋 No checkpoint found, starting from beginning")
            return 1  # Start from row 2 (row 1 is header)
        except Exception as e:
            logger.warning(f"⚠️  Error loading checkpoint: {e}. Starting from beginning.")
            return 1
    
    def save_checkpoint(self, row_index):
        """Save checkpoint"""
        try:
            # Ensure checkpoint directory exists
            checkpoint_dir = os.path.dirname(self.checkpoint_file)
            if checkpoint_dir and not os.path.exists(checkpoint_dir):
                os.makedirs(checkpoint_dir, exist_ok=True)
            
            with open(self.checkpoint_file, 'w') as f:
                f.write(str(row_index))
        except Exception as e:
            logger.warning(f"⚠️  Error saving checkpoint: {e}")
    
    def update_google_sheet(self, row_index, result):
        """Update Google Sheet with search results"""
        try:
            # Update Post Exist? column
            self.worksheet.update_cell(row_index, self.post_exist_col, result['status'])
            
            # Update Twitter Handle column
            if result['username']:
                self.worksheet.update_cell(row_index, self.twitter_handle_col, f"@{result['username']}")
            else:
                self.worksheet.update_cell(row_index, self.twitter_handle_col, "")
            
            # Update Confidence Score column (now stores: High, Medium, Low, None)
            if result['confidence'] is not None:
                self.worksheet.update_cell(row_index, self.confidence_col, result['confidence'])
            else:
                self.worksheet.update_cell(row_index, self.confidence_col, "None")
            
            # Update Script Run column (Column 8) - mark as processed
            self.worksheet.update_cell(row_index, self.script_run_col, "true")
            
            logger.info(f"   💾 Updated row {row_index} in Google Sheets")
        except Exception as e:
            logger.error(f"   ⚠️  Error updating Google Sheets: {e}")
    
    async def process_wallet_with_semaphore(self, row_index, wallet):
        """Process a single wallet with semaphore for rate limiting"""
        async with self.semaphore:
            logger.info(f"🔍 Processing wallet (Row {row_index}): {wallet[:20]}...")
            
            # Check wallet with GROK
            result = await self.check_wallet(wallet)
            
            if result:
                result['row'] = row_index
                result['wallet'] = wallet
                
                # Update Google Sheet
                self.update_google_sheet(row_index, result)
                
                return result
            return None
    
    async def process_wallets(self, limit=None, start_from=None, use_parallel=True):
        """Process wallets from Google Sheets with optional parallel processing"""
        # Get limit from environment or use default
        if limit is None:
            limit = int(os.environ.get("WALLET_LIMIT", "5"))  # Default to 5 for testing
        
        # Get all data
        all_data = self.worksheet.get_all_values()
        headers = all_data[0]
        data_rows = all_data[1:]
        
        if start_from is None:
            start_from = self.load_checkpoint()
        
        # Adjust for 0-based indexing (row 1 is header, row 2 is first data)
        start_index = max(0, start_from - 2)
        
        # If limit is 0 or negative, process all remaining wallets
        if limit <= 0:
            end_index = len(data_rows)
        else:
            end_index = min(len(data_rows), start_index + limit)
        
        wallets_to_process = []
        for i in range(start_index, end_index):
            if i < len(data_rows):
                wallet = data_rows[i][self.wallet_col - 1] if self.wallet_col <= len(data_rows[i]) else ""
                if wallet and wallet.strip():
                    wallets_to_process.append((i + 2, wallet.strip()))  # +2 because row 1 is header
        
        logger.info(f"🚀 Starting GROK search for {len(wallets_to_process)} wallets")
        logger.info(f"   Starting from row {start_from}")
        logger.info(f"   Total rows in sheet: {len(data_rows)}")
        logger.info(f"   Parallel processing: {use_parallel} (max concurrent: {self.max_concurrent})")
        logger.info("=" * 60)
        
        results = []
        start_time = time.time()
        processed_count = 0
        
        if use_parallel and len(wallets_to_process) > 1:
            # Parallel processing
            batch_size = self.max_concurrent * 2  # Process in batches
            
            for batch_start in range(0, len(wallets_to_process), batch_size):
                batch = wallets_to_process[batch_start:batch_start + batch_size]
                logger.info(f"\n📦 Processing batch {batch_start // batch_size + 1} ({len(batch)} wallets)...")
                
                # Create tasks for this batch
                tasks = [
                    self.process_wallet_with_semaphore(row_index, wallet)
                    for row_index, wallet in batch
                ]
                
                # Process batch in parallel
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Process results
                for i, result in enumerate(batch_results):
                    if isinstance(result, Exception):
                        logger.error(f"   ❌ Error processing wallet: {result}")
                        continue
                    
                    if result:
                        results.append(result)
                        processed_count += 1
                        
                        # Save checkpoint after each successful processing
                        self.save_checkpoint(result['row'] + 1)
                
                # Small delay between batches to avoid overwhelming API
                if batch_start + batch_size < len(wallets_to_process):
                    await asyncio.sleep(1)
        else:
            # Sequential processing (fallback or for single wallet)
            for idx, (row_index, wallet) in enumerate(wallets_to_process):
                logger.info(f"\n[{idx + 1}/{len(wallets_to_process)}] Processing wallet (Row {row_index})...")
                
                result = await self.process_wallet_with_semaphore(row_index, wallet)
                
                if result:
                    results.append(result)
                    processed_count += 1
                    self.save_checkpoint(row_index + 1)
        
        elapsed_time = time.time() - start_time
        logger.info(f"\n✅ Completed search for {processed_count} wallets")
        logger.info(f"   Total time: {elapsed_time:.2f} seconds ({elapsed_time/60:.2f} minutes)")
        if processed_count > 0:
            logger.info(f"   Average time per wallet: {elapsed_time/processed_count:.2f} seconds")
        
        return results

async def process_worksheet(worksheet_name, limit=None, shared_semaphore=None):
    """Process a single worksheet
    
    Args:
        worksheet_name: Name of the worksheet to process
        limit: Optional limit on number of wallets to process
        shared_semaphore: Optional shared semaphore for cross-worksheet concurrency control
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"📊 Processing Worksheet: {worksheet_name}")
    logger.info(f"{'='*60}")
    
    try:
        # Initialize searcher for this worksheet with shared semaphore
        searcher = GrokWalletSearcher(worksheet_name=worksheet_name, shared_semaphore=shared_semaphore)
        
        # Get limit from environment (0 or negative = process all)
        if limit is None:
            limit = int(os.environ.get("WALLET_LIMIT", "0"))  # Default to 0 (all)
        
        logger.info(f"Processing limit: {limit if limit > 0 else 'ALL'} wallets")
        
        # Determine if we should use parallel processing
        use_parallel = os.environ.get("USE_PARALLEL", "true").lower() == "true"
        
        # Process wallets
        results = await searcher.process_wallets(limit=limit, use_parallel=use_parallel)
        
        # Print summary
        logger.info(f"\n📊 SUMMARY for {worksheet_name}:")
        logger.info("=" * 30)
        found_count = sum(1 for r in results if r and r.get('status') == 'true')
        no_count = sum(1 for r in results if r and r.get('status') == 'false')
        error_count = sum(1 for r in results if r and r.get('status') == 'Error')
        
        logger.info(f"   Wallets searched: {len(results)}")
        logger.info(f"   Posts found: {found_count}")
        logger.info(f"   No posts: {no_count}")
        logger.info(f"   Errors: {error_count}")
        
        # Show results with usernames
        if found_count > 0:
            logger.info(f"\n📋 Wallets with posts found:")
            for result in results[:10]:  # Show first 10
                if result and result.get('status') == 'true':
                    logger.info(f"   Row {result['row']}: {result['wallet'][:20]}...")
                    logger.info(f"      Username: @{result['username'] if result.get('username') else 'N/A'}")
                    logger.info(f"      Confidence: {result['confidence'] if result.get('confidence') else 'N/A'}")
            if found_count > 10:
                logger.info(f"   ... and {found_count - 10} more")
        
        return results
        
    except Exception as e:
        logger.error(f"❌ Error processing {worksheet_name}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []

async def main():
    """Main function - Railway-compatible batch job with multi-worksheet support"""
    logger.info("🤖 GROK Wallet Search via x.ai SDK")
    logger.info("=" * 50)
    logger.info(f"Environment: {'Railway' if os.environ.get('RAILWAY_ENVIRONMENT') else 'Local'}")
    
    try:
        # Get worksheets to process
        worksheets_to_process = os.environ.get("WORKSHEETS_TO_PROCESS", "")
        
        if worksheets_to_process:
            # Process multiple worksheets
            worksheet_names = [w.strip() for w in worksheets_to_process.split(",")]
            logger.info(f"Processing {len(worksheet_names)} worksheets: {', '.join(worksheet_names)}")
        else:
            # Process single worksheet (default or from WORKSHEET_NAME)
            worksheet_name = os.environ.get("WORKSHEET_NAME", "Gigabud Holders")
            worksheet_names = [worksheet_name]
            logger.info(f"Processing single worksheet: {worksheet_name}")
        
        all_results = {}
        
        # Create shared semaphore for cross-worksheet concurrency control
        # This ensures total concurrent requests across all worksheets don't exceed MAX_CONCURRENT_REQUESTS
        max_concurrent = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "5"))
        shared_semaphore = asyncio.Semaphore(max_concurrent)
        logger.info(f"🔒 Shared concurrency limit: {max_concurrent} requests across all worksheets")
        
        # Check if we should process worksheets in parallel
        use_parallel_worksheets = os.environ.get("USE_PARALLEL", "true").lower() == "true" and len(worksheet_names) > 1
        
        if use_parallel_worksheets:
            # Process worksheets in parallel with shared semaphore
            logger.info(f"🚀 Processing {len(worksheet_names)} worksheets in parallel...")
            logger.info(f"   Total concurrent requests limited to {max_concurrent} across all worksheets")
            tasks = [
                process_worksheet(worksheet_name, shared_semaphore=shared_semaphore)
                for worksheet_name in worksheet_names
                if worksheet_name
            ]
            
            # Process all worksheets concurrently
            worksheet_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Map results to worksheet names
            for i, result in enumerate(worksheet_results):
                worksheet_name = worksheet_names[i]
                if isinstance(result, Exception):
                    logger.error(f"❌ Error processing {worksheet_name}: {result}")
                    all_results[worksheet_name] = []
                else:
                    all_results[worksheet_name] = result
        else:
            # Process worksheets sequentially (original behavior)
            for worksheet_name in worksheet_names:
                if not worksheet_name:
                    continue
                    
                results = await process_worksheet(worksheet_name, shared_semaphore=shared_semaphore)
                all_results[worksheet_name] = results
                
                # Small delay between worksheets
                if worksheet_name != worksheet_names[-1]:
                    logger.info("\n⏳ Waiting 5 seconds before next worksheet...")
                    await asyncio.sleep(5)
        
        # Final summary
        logger.info(f"\n{'='*60}")
        logger.info("📊 FINAL SUMMARY")
        logger.info(f"{'='*60}")
        
        total_wallets = sum(len(r) for r in all_results.values())
        total_found = sum(sum(1 for w in r if w and w.get('status') == 'true') for r in all_results.values())
        total_no_posts = sum(sum(1 for w in r if w and w.get('status') == 'false') for r in all_results.values())
        
        logger.info(f"   Total worksheets processed: {len(all_results)}")
        logger.info(f"   Total wallets searched: {total_wallets}")
        logger.info(f"   Total posts found: {total_found}")
        logger.info(f"   Total no posts: {total_no_posts}")
        
        logger.info("\n✅ All jobs completed successfully")
        
    except KeyboardInterrupt:
        logger.warning("\n⏹️  Search interrupted by user")
        logger.info("💾 Progress saved. Resume by running again.")
        raise  # Re-raise to exit with error code
    except Exception as e:
        logger.error(f"\n❌ Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise  # Re-raise to exit with error code for Railway

if __name__ == "__main__":
    asyncio.run(main())
