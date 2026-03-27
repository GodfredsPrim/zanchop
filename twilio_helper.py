import os
import logging
from twilio.rest import Client
from twilio.rest.content.v1.content import ContentList
from dotenv import load_dotenv

load_dotenv()

# Configuration
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER") # e.g., 'whatsapp:+14155238886'
CONTENT_SID = os.getenv("TWILIO_CONTENT_SID") # Optional: Pre-registered content template SID

client = None
if ACCOUNT_SID and AUTH_TOKEN:
    client = Client(ACCOUNT_SID, AUTH_TOKEN)

def send_whatsapp(to_phone, message, media_url=None):
    """
    Send a WhatsApp message via Twilio.
    to_phone: string (e.g., '+233241234567')
    message: string (body text)
    media_url: optional list or string for images
    """
    if not client:
        logging.error("Twilio client not initialized. Check .env file.")
        return False

    try:
        # Ensure the phone number starts with 'whatsapp:'
        to = to_phone if to_phone.startswith('whatsapp:') else f'whatsapp:{to_phone}'
        
        params = {
            "from_": WHATSAPP_NUMBER,
            "to": to,
            "body": message
        }
        
        if media_url:
            params["media_url"] = [media_url] if isinstance(media_url, str) else media_url

        message_instance = client.messages.create(**params)
        logging.info(f"✅ Message sent to {to}. SID: {message_instance.sid}")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to send WhatsApp message to {to_phone}: {e}")
        return False

def send_whatsapp_with_buttons(to_phone, header, body, buttons):
    """
    Send a WhatsApp message with formatted button-style menu.
    This shows beautifully formatted menus that users can select by number.
    
    to_phone: string (e.g., '+233241234567')
    header: string (title of the message)
    body: string (main message text)
    buttons: list of dicts with 'title' and 'id' keys
    """
    if not client:
        logging.error("Twilio client not initialized. Check .env file.")
        return False

    try:
        # Ensure the phone number starts with 'whatsapp:'
        to = to_phone if to_phone.startswith('whatsapp:') else f'whatsapp:{to_phone}'
        
        # Build formatted message with button-like display
        formatted_body = f"*{header}*\n\n{body}\n\n"
        formatted_body += "━━━━━━━━━━━━━━━━━\n"
        for i, btn in enumerate(buttons[:4]):
            formatted_body += f"▶️  [{i+1}] {btn['title']}\n"
        formatted_body += "━━━━━━━━━━━━━━━━━\n"
        formatted_body += "💬 Tap a number to select"
        
        message_instance = client.messages.create(
            from_=WHATSAPP_NUMBER,
            to=to,
            body=formatted_body
        )
        
        logging.info(f"✅ Menu sent to {to}. SID: {message_instance.sid}")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to send menu to {to_phone}: {e}")
        return False
