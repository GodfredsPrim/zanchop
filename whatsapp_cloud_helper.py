import os
import requests
import json
import logging
from dotenv import load_dotenv

load_dotenv()

# Meta WhatsApp Cloud API Config
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
API_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

def _build_headers():
    return {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

def _log_meta_error(action, response):
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}

    error = payload.get("error", {})
    message = error.get("message") or payload
    code = error.get("code")
    subcode = error.get("error_subcode")
    logging.error(f"❌ {action} failed: HTTP {response.status_code} | code={code} | subcode={subcode} | {message}")

def _post_to_meta(payload, action):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        logging.error("Cloud API credentials missing.")
        return False
    response = requests.post(API_URL, headers=_build_headers(), json=payload, timeout=30)
    if response.ok:
        return True
    _log_meta_error(action, response)
    return False

def send_whatsapp_message(to, body, headers=None):
    """Fallback: Send standard text message via Cloud API."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body}
    }

    try:
        success = _post_to_meta(payload, "Text message")
        if success:
            logging.info(f"✅ Text message sent to {to}")
        return success
    except Exception as e:
        logging.error(f"❌ Failed to send Cloud API message: {e}")
        return False

def send_interactive_buttons(to, body, buttons, header_text=None):
    """
    Send native WhatsApp buttons.
    buttons: List of dicts [{"id": "btn1", "title": "Option 1"}, ...]
    Max 3 buttons.
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": b["id"], "title": b["title"]}
                    } for b in buttons[:3]
                ]
            }
        }
    }

    if header_text:
        payload["interactive"]["header"] = {"type": "text", "text": header_text}

    try:
        return _post_to_meta(payload, "Buttons")
    except Exception as e:
        logging.error(f"❌ Failed to send buttons: {e}")
        return False

def send_interactive_list(to, body, button_label, sections, header_text=None):
    """
    Send native WhatsApp list menu.
    sections: List of dicts [{"title": "Shop A", "rows": [{"id": "s1", "title": "Rice Shop", "description": "Good food"}]}, ...]
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_label,
                "sections": sections
            }
        }
    }

    if header_text:
        payload["interactive"]["header"] = {"type": "text", "text": header_text}

    try:
        return _post_to_meta(payload, "List")
    except Exception as e:
        logging.error(f"❌ Failed to send list: {e}")
        return False

def send_whatsapp_image(to, image_url, caption=None):
    """
    Send an image via WhatsApp Cloud API.
    image_url: Must be a publicly accessible URL
    caption: Optional caption text
    """
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "image",
        "image": {
            "link": image_url
        }
    }
    
    if caption:
        payload["image"]["caption"] = caption

    try:
        success = _post_to_meta(payload, "Image")
        if success:
            logging.info(f"✅ Image sent to {to}")
        return success
    except Exception as e:
        logging.error(f"❌ Failed to send Cloud API image: {e}")
        return False
