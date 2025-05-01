import httpx
import logging
import pytz
import os
from datetime import datetime
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional, Union

from fastapi import FastAPI, status, HTTPException
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.exc import SQLAlchemyError

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level               = logging.INFO
    , format            = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    , filename          = "market_monitor.log"
)
logger                  = logging.getLogger("market_monitor")

API_BASE_URL            = os.getenv("API_BASE_URL")
TELEGRAM_API_ENDPOINT   = f"{API_BASE_URL}/api/v1/telegram/send/market"
API_TOKEN               = os.getenv("API_TOKEN")

# Pydantic models for type safety
class MarketData(BaseModel):
    code                : str
    name                : str
    local_time          : str
    phnom_penh_time     : str
    notes               : Optional[str] = None
    status              : str
    local_date          : Optional[str] = None

class NotificationPayload(BaseModel):
    message             : str

def get_phnom_penh_time(
    timezone_str         : str
    , local_time         : str
) -> str:
    """Convert a local market time to Phnom Penh time using pytz"""
    hours, minutes       = map(int, local_time.split(':'))
    
    market_tz            = pytz.timezone(timezone_str)
    cambodia_tz          = pytz.timezone('Asia/Phnom_Penh')
    
    today                = datetime.now(market_tz).replace(hour=hours, minute=minutes, second=0, microsecond=0)
    
    cambodia_time        = today.astimezone(cambodia_tz)
    
    # Return in HH:MM format
    return cambodia_time.strftime("%H:%M")

MARKETS = {
    "NYSE": {
        "name"          : "New York Stock Exchange (NYSE)"
        , "timezone"    : "America/New_York"
        , "open_time"   : "09:30"  # 24-hour format
        , "close_time"  : "16:00"
        , "notes"       : "Pre-market trading from 4:00 AM – 9:30 AM ET; after-hours trading until 8:00 PM ET."
        , "trading_days": "1,2,3,4,5"  # Monday to Friday
        , "icon"        : "🗽"
    },
    "LSE": {
        "name"          : "London Stock Exchange (LSE)"
        , "timezone"    : "Europe/London"
        , "open_time"   : "08:00"
        , "close_time"  : "16:30"
        , "notes"       : "No lunch break; operates Monday to Friday."
        , "trading_days": "1,2,3,4,5"  # Monday to Friday
        , "icon"        : "🇬🇧"
    },
    "TSE": {
        "name"          : "Tokyo Stock Exchange (TSE)"
        , "timezone"    : "Asia/Tokyo"
        , "open_time"   : "09:00"
        , "close_time"  : "15:00"
        , "notes"       : "Lunch break from 11:30 AM – 12:30 PM JST."
        , "trading_days": "1,2,3,4,5"  # Monday to Friday
        , "icon"        : "🇯🇵"
    },
    "HKEX": {
        "name"          : "Hong Kong Stock Exchange (HKEX)"
        , "timezone"    : "Asia/Hong_Kong"
        , "open_time"   : "09:30"
        , "close_time"  : "16:00"
        , "notes"       : "Lunch break from 12:00 PM – 1:00 PM HKT."
        , "trading_days": "1,2,3,4,5"  # Monday to Friday
        , "icon"        : "🇭🇰"
    },
    "ASX": {
        "name"          : "Australian Securities Exchange (ASX)"
        , "timezone"    : "Australia/Sydney"
        , "open_time"   : "10:00"
        , "close_time"  : "16:00"
        , "notes"       : "Opening auction starts at 10:00 AM AEST; no lunch break."
        , "trading_days": "1,2,3,4,5"  # Monday to Friday
        , "icon"        : "🇦🇺"
    }
}

# Calculate Phnom Penh times for each market dynamically
for code, market in MARKETS.items():
    market["phnom_penh_time_open"]   = get_phnom_penh_time(market["timezone"], market["open_time"])
    market["phnom_penh_time_close"]  = get_phnom_penh_time(market["timezone"], market["close_time"])
    logger.info(f"{code}: Local {market['open_time']}-{market['close_time']} = Phnom Penh {market['phnom_penh_time_open']}-{market['phnom_penh_time_close']}")

# Create scheduler
scheduler = AsyncIOScheduler()

async def send_notification(
    event_type          : str
    , markets_data      : List[Dict[str, Any]]
    , is_test           : bool = False
) -> Union[bool, Dict[str, Any]]:
    """Send notification to Telegram API with well-formatted message"""
    try:
        cambodia_tz     = pytz.timezone('Asia/Phnom_Penh')
        now             = datetime.now(cambodia_tz)
        
        message_parts   = []
        
        # Add header based on event type
        if event_type == "market_opening":
            message_parts.append("🔔 **MARKET OPENING SOON**")
        elif event_type == "market_closing":
            message_parts.append("🔔 **MARKET CLOSING SOON**")
        elif event_type == "daily_summary":
            message_parts.append("📊 **DAILY MARKET SCHEDULE**")
            message_parts.append(f"Today: {now.strftime('%A, %d %B %Y')}")
            message_parts.append(f"Current time in Phnom Penh: {now.strftime('%H:%M')}")
        
        for market in markets_data:
            market_code = market.get("code", "")
            icon        = MARKETS.get(market_code, {}).get("icon", "🏢")
            
            if event_type == "market_opening":
                market_msg = f"{icon} **{market['name']}** opening in 5 minutes"
                market_msg += f"\n* **Local Time:** {market['local_time'].split(' – ')[0]}"
                market_msg += f"\n* **Phnom Penh Time:** {market['phnom_penh_time'].split(' – ')[0]}"
            elif event_type == "market_closing":
                market_msg = f"{icon} **{market['name']}** closing in 5 minutes"
                market_msg += f"\n* **Local Time:** {market['local_time'].split(' – ')[1]}"
                market_msg += f"\n* **Phnom Penh Time:** {market['phnom_penh_time'].split(' – ')[1]}"
            else:
                # Daily summary format with status indicator
                status_indicator = "🟢" if market['status'] == "trading" else "🔴"
                market_msg = f"{icon} **{market['name']}** {status_indicator}"
                market_msg += f"\n* **Local Time:** {market['local_time']}"
                market_msg += f"\n* **Phnom Penh Time:** {market['phnom_penh_time']}"
                if market.get("notes"):
                    market_msg += f"\n* **Notes:** {market['notes']}"
            
            message_parts.append(market_msg)
        
        formatted_message = "\n\n".join(message_parts)
        
        # New simplified notification data with only the message
        notification_data = NotificationPayload(
            message = formatted_message
        ).model_dump()

        logger.info(f"Sending {event_type} notification with message:\n{formatted_message}")
        
        # For test mode, just return the notification data without sending it
        if is_test:
            return notification_data
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                TELEGRAM_API_ENDPOINT
                , json      = notification_data
                , headers   = {"Authorization": f"Bearer {API_TOKEN}"}
            )
            
            if response.status_code != 200:
                logger.error(f"Failed to send notification: {response.text}")
                return False
            
            logger.info(f"Notification sent for {event_type} event")
            return True
            
    except Exception as e:
        logger.exception(f"Error sending {event_type} notification")
        return False

async def check_market_opening(
    market_code         : str
    , is_test           : bool = False
) -> Union[None, Dict[str, Any]]:
    """Check if a specific market is opening now and send notification"""
    cambodia_tz         = pytz.timezone('Asia/Phnom_Penh')
    now                 = datetime.now(cambodia_tz)
    
    weekday             = str(now.weekday() + 1)  # Convert to 1-7 format (Monday is 1)
    market              = MARKETS[market_code]
    
    if weekday not in market["trading_days"].split(",") and not is_test:
        logger.debug(f"{market_code} does not trade today (weekday {weekday})")
        return
    
    market_tz           = pytz.timezone(market["timezone"])
    
    market_local_now    = now.astimezone(market_tz)
    logger.info(f"Market {market_code} opening check - Local time: {market_local_now.strftime('%H:%M')}")
    
    market_data = [{
        "code"              : market_code
        , "name"            : market["name"]
        , "local_time"      : f"{market['open_time']} – {market['close_time']}"
        , "phnom_penh_time" : f"{market['phnom_penh_time_open']} – {market['phnom_penh_time_close']}"
        , "notes"           : market["notes"]
        , "status"          : "opening"
    }]
    
    # Send opening notification
    return await send_notification("market_opening", market_data, is_test)

async def check_market_closing(
    market_code         : str
    , is_test           : bool = False
) -> Union[None, Dict[str, Any]]:
    """Check if a specific market is closing now and send notification"""
    cambodia_tz         = pytz.timezone('Asia/Phnom_Penh')
    now                 = datetime.now(cambodia_tz)
    
    weekday             = str(now.weekday() + 1) 
    market              = MARKETS[market_code]
    
    if weekday not in market["trading_days"].split(",") and not is_test:
        logger.debug(f"{market_code} does not trade today (weekday {weekday})")
        return
    
    market_tz           = pytz.timezone(market["timezone"])    
    
    market_local_now    = now.astimezone(market_tz)
    logger.info(f"Market {market_code} closing check - Local time: {market_local_now.strftime('%H:%M')}")
    
    market_data = [{
        "code"              : market_code
        , "name"            : market["name"]
        , "local_time"      : f"{market['open_time']} – {market['close_time']}"
        , "phnom_penh_time" : f"{market['phnom_penh_time_open']} – {market['phnom_penh_time_close']}"
        , "notes"           : market["notes"]
        , "status"          : "closing"
    }]
    
    # Send closing notification
    return await send_notification("market_closing", market_data, is_test)

async def daily_summary(
    is_test             : bool = False
) -> Union[None, Dict[str, Any]]:
    """Send a daily summary of all market schedules with accurate time conversions"""
    
    cambodia_tz         = pytz.timezone('Asia/Phnom_Penh')
    now                 = datetime.now(cambodia_tz)
    weekday             = str(now.weekday() + 1)
    
    markets_data        = []
    for code, market in MARKETS.items():
        is_trading_today = weekday in market["trading_days"].split(",")
        
        market_tz           = pytz.timezone(market["timezone"])
        
        market_local_now    = now.astimezone(market_tz)
        local_date          = market_local_now.strftime("%Y-%m-%d")
        
        markets_data.append({
            "code"              : code
            , "name"            : market["name"]
            , "local_time"      : f"{market['open_time']} – {market['close_time']} ({market['timezone'].split('/')[-1]})"
            , "phnom_penh_time" : f"{market['phnom_penh_time_open']} – {market['phnom_penh_time_close']} (Phnom Penh)"
            , "local_date"      : local_date
            , "notes"           : market["notes"]
            , "status"          : "trading" if is_trading_today else "closed"
        })
    
    markets_data.sort(key=lambda x: (0 if x["status"] == "trading" else 1, x["name"]))
    
    return await send_notification("daily_summary", markets_data, is_test)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Market monitor starting")
    
    for code, market in MARKETS.items():
        open_time       = market["phnom_penh_time_open"]
        open_hour, open_minute = map(int, open_time.split(":"))
        
        if open_minute >= 5:
            open_minute -= 5
        else:
            open_hour   = (open_hour - 1) % 24
            open_minute = 55
        
        scheduler.add_job(
            check_market_opening
            , CronTrigger(
                hour        = open_hour
                , minute    = open_minute
                , timezone  = pytz.timezone('Asia/Phnom_Penh')
            )
            , args=[code]
        )
        logger.info(f"Scheduled opening notification for {code} at {open_hour}:{open_minute:02d}")
        
        close_time      = market["phnom_penh_time_close"]
        close_hour, close_minute = map(int, close_time.split(":"))
        
        if close_minute >= 5:
            close_minute -= 5
        else:
            close_hour   = (close_hour - 1) % 24
            close_minute = 55
        
        scheduler.add_job(
            check_market_closing
            , CronTrigger(
                hour        = close_hour
                , minute    = close_minute
                , timezone  = pytz.timezone('Asia/Phnom_Penh')
            )
            , args=[code]
        )
        logger.info(f"Scheduled closing notification for {code} at {close_hour}:{close_minute:02d}")
    
    scheduler.add_job(
        daily_summary
        , CronTrigger(
            hour        = 6
            , minute    = 0
            , timezone  = pytz.timezone('Asia/Phnom_Penh')
        )
    )
    logger.info("Scheduled daily market summary at 6:00 AM")
    
    # Start the scheduler
    scheduler.start()
    
    yield

    # Shutdown scheduler
    scheduler.shutdown()
    logger.info("Market monitor shutting down")

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root() -> Dict[str, str]:
    """Root endpoint to check if service is running"""
    return {"status": "Market monitor running"}

@app.post("/test/notification/{event_type}")
async def test_notification(
    event_type          : str
    , market_code       : Optional[str] = None
) -> Dict[str, Any]:
    try:
        if event_type not in ["market_opening", "market_closing", "daily_summary"]:
            raise HTTPException(
                status_code = status.HTTP_400_BAD_REQUEST
                , detail    = "Invalid event type. Must be 'market_opening', 'market_closing', or 'daily_summary'"
            )
            
        if event_type == "daily_summary":
            result = await daily_summary(is_test=True)
            return result
        
        if not market_code:
            raise HTTPException(
                status_code = status.HTTP_400_BAD_REQUEST
                , detail    = "Market code is required for market opening/closing notifications"
            )
            
        if market_code not in MARKETS:
            raise HTTPException(
                status_code = status.HTTP_404_NOT_FOUND
                , detail    = f"Market code '{market_code}' not found"
            )
            
        if event_type == "market_opening":
            result = await check_market_opening(market_code, is_test=True)
            return result
        
        # Must be market_closing at this point
        result = await check_market_closing(market_code, is_test=True)
        return result
    
    except Exception as e:
        logger.exception(f"Error triggering test notification: {str(e)}")
        raise HTTPException(
            status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
            , detail    = f"Error triggering notification: {str(e)}"
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)