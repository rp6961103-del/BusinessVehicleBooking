import base64
import io
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
import requests

logger = logging.getLogger(__name__)

# Allowed image extensions and MIME types
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

DEFAULT_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_TIMEOUT = 30
MAX_GEMINI_RETRIES = 2
AI_UNAVAILABLE_MESSAGE = "AI disease detection is currently unavailable. Please try again."
SYSTEM_INSTRUCTION = (
    "You are an agricultural crop-disease assistant. Analyze the uploaded plant leaf image. "
    "Identify the crop and provide the most likely visible disease or condition only when supported by visual evidence. "
    "Do not claim certainty. If the image is unclear or does not show a plant/crop leaf, say that the image is insufficient. "
    "Explain the visible symptoms in simple language. Provide general crop-management and prevention guidance. "
    "For pesticides, fungicides, insecticides, or medicines, do not provide unsafe or unsupported dosing. "
    "Recommend verifying chemical selection, concentration, crop suitability, and label directions with a qualified "
    "agricultural expert or local agriculture department. Return the result in structured JSON."
)

JSON_SCHEMA_HINT = """
Return ONLY a valid JSON object matching this schema:
{
  "crop": "Name of the crop or plant (e.g., Tomato, Rice, Cotton, Chilli, Wheat, or 'Unclear / Not a plant')",
  "possible_disease": "Name of the likely condition or 'Healthy' or 'Unclear / Insufficient image quality'",
  "confidence": "Estimated confidence percentage or range (e.g., '85%' or 'Moderate (70-80%)')",
  "symptoms": "Description of visible signs and symptoms on the leaf in simple language",
  "possible_causes": "Likely causes (fungal, bacterial, viral, nutrient deficiency, pest damage, weather)",
  "immediate_steps": "Recommended safe immediate steps the farmer should take",
  "management": "General treatment and crop management recommendations",
  "prevention": "Preventive measures for future crops and spread control",
  "expert_advice": "When and why to consult a local agricultural officer or extension specialist"
}
"""


def is_ai_configured() -> bool:
    """Check if an AI API key is configured in the environment."""
    api_key = (
        os.getenv("AI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
    )
    return bool(api_key and api_key.strip())


def get_ai_config() -> Dict[str, str]:
    """Retrieve active AI configuration from environment variables."""
    api_key = (
        os.getenv("AI_API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    provider = os.getenv("AI_PROVIDER", "").strip().lower()
    if not provider:
        if os.getenv("OPENAI_API_KEY") and not os.getenv("AI_API_KEY") and not os.getenv("GEMINI_API_KEY"):
            provider = "openai"
        else:
            provider = "gemini"

    model = os.getenv("AI_MODEL", "").strip()
    if not model:
        model = "gpt-4o-mini" if provider == "openai" else DEFAULT_MODEL

    base_url = os.getenv("AI_BASE_URL", "").strip()
    return {
        "api_key": api_key,
        "provider": provider,
        "model": model,
        "base_url": base_url,
    }


def validate_image_file(file_storage) -> Tuple[bool, Optional[str], Optional[bytes], Optional[str]]:
    """
    Validate uploaded file format, size, and image integrity using Pillow.
    Returns: (is_valid, error_message, image_bytes, mime_type)
    """
    if not file_storage or not file_storage.filename:
        return False, "Please select an image file to upload.", None, None

    filename = file_storage.filename.lower()
    extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
    if extension not in ALLOWED_EXTENSIONS:
        return False, "Invalid image type. Only JPG, JPEG, PNG, and WEBP formats are accepted.", None, None

    try:
        image_bytes = file_storage.read()
    except Exception as err:
        logger.warning("Failed to read uploaded file: %s", err)
        return False, "Unable to read the uploaded image.", None, None

    if not image_bytes or len(image_bytes) == 0:
        return False, "Uploaded image is empty.", None, None

    if len(image_bytes) > MAX_FILE_SIZE:
        return False, f"Image file is too large. Maximum allowed size is {MAX_FILE_SIZE // (1024 * 1024)} MB.", None, None

    # Verify actual image content with Pillow
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.verify()
            fmt = (img.format or "").upper()
            mime_map = {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }
            mime_type = mime_map.get(fmt)
            if not mime_type:
                return False, "Unsupported image encoding. Please upload a standard JPG, PNG, or WEBP image.", None, None
    except Exception as err:
        logger.warning("Corrupt or invalid image uploaded: %s", err)
        return False, "The file could not be recognized as a valid image. Please try another photo.", None, None

    return True, None, image_bytes, mime_type


def _clean_json_text(text: str) -> str:
    """Extract raw JSON text by removing markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text)
    return text.strip()


def _gemini_timeout() -> int:
    """Return a bounded request timeout from the environment."""
    try:
        timeout = int(os.getenv("GEMINI_TIMEOUT", str(DEFAULT_GEMINI_TIMEOUT)))
    except ValueError:
        timeout = DEFAULT_GEMINI_TIMEOUT
    return max(1, timeout)


def _call_gemini_api(api_key: str, model: str, prompt: str, image_bytes: Optional[bytes] = None, mime_type: Optional[str] = None) -> str:
    """Call Google Gemini generateContent REST API."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    parts: List[Dict[str, Any]] = [{"text": prompt}]

    if image_bytes and mime_type:
        encoded_img = base64.b64encode(image_bytes).decode("utf-8")
        parts.append({
            "inline_data": {
                "mime_type": mime_type,
                "data": encoded_img,
            }
        })

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1500,
        }
    }

    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    timeout = _gemini_timeout()
    for attempt in range(1, MAX_GEMINI_RETRIES + 2):
        try:
            logger.info(
                "Gemini image analysis request attempt=%s timeout=%ss",
                attempt,
                timeout,
            )
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
        except (requests.Timeout, requests.ConnectionError) as err:
            if attempt <= MAX_GEMINI_RETRIES:
                logger.warning(
                    "Gemini request transient failure attempt=%s type=%s; retrying",
                    attempt,
                    type(err).__name__,
                )
                time.sleep(min(attempt, 2))
                continue
            logger.error(
                "Gemini request failed after %s attempts type=%s",
                attempt,
                type(err).__name__,
            )
            raise RuntimeError("Gemini request failed after retries") from err

        if response.status_code == 200:
            data = response.json()
            break

        retryable = response.status_code == 429 or 500 <= response.status_code <= 599
        if retryable and attempt <= MAX_GEMINI_RETRIES:
            logger.warning(
                "Gemini API transient HTTP failure attempt=%s status=%s; retrying",
                attempt,
                response.status_code,
            )
            time.sleep(min(attempt, 2))
            continue

        logger.error(
            "Gemini API request failed attempt=%s status=%s retryable=%s",
            attempt,
            response.status_code,
            retryable,
        )
        raise RuntimeError(f"Gemini API returned status {response.status_code}")

    try:
        candidates = data.get("candidates", [])
        if not candidates:
            raise RuntimeError("No response candidate returned from AI model.")
        text_content = candidates[0]["content"]["parts"][0]["text"]
        return text_content
    except (KeyError, IndexError) as err:
        raise RuntimeError(f"Unexpected response structure from AI model: {err}")


def _call_openai_api(api_key: str, model: str, base_url: str, prompt: str, image_bytes: Optional[bytes] = None, mime_type: Optional[str] = None) -> str:
    """Call OpenAI-compatible vision chat completions endpoint."""
    endpoint = (base_url.rstrip("/") if base_url else "https://api.openai.com/v1") + "/chat/completions"
    user_content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]

    if image_bytes and mime_type:
        encoded_img = base64.b64encode(image_bytes).decode("utf-8")
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded_img}"}
        })

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(endpoint, json=payload, headers=headers, timeout=30)
    if response.status_code != 200:
        error_msg = f"AI API returned status {response.status_code}"
        try:
            err_data = response.json()
            if "error" in err_data and "message" in err_data["error"]:
                error_msg += f": {err_data['error']['message']}"
        except Exception:
            pass
        raise RuntimeError(error_msg)

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as err:
        raise RuntimeError(f"Unexpected response structure from AI model: {err}")


def analyze_crop_leaf(image_bytes: bytes, mime_type: str, language: str = "en") -> Dict[str, Any]:
    """
    Analyzes an uploaded leaf image and returns structured disease identification.
    Safe fallbacks if AI is unconfigured or if API errors occur.
    """
    if not is_ai_configured():
        return {
            "success": False,
            "configured": False,
            "error": AI_UNAVAILABLE_MESSAGE,
        }

    config = get_ai_config()

    language_instruction = ""
    if language == "te":
        language_instruction = (
            "IMPORTANT: Output all textual explanations (crop, possible_disease, symptoms, possible_causes, "
            "immediate_steps, management, prevention, expert_advice) in Telugu language (తెలుగు). Keep JSON keys in English."
        )
    elif language == "hi":
        language_instruction = (
            "IMPORTANT: Output all textual explanations (crop, possible_disease, symptoms, possible_causes, "
            "immediate_steps, management, prevention, expert_advice) in Hindi language (हिन्दी). Keep JSON keys in English."
        )
    else:
        language_instruction = "Output all textual explanations in clear, farmer-friendly English."

    prompt = f"{SYSTEM_INSTRUCTION}\n\n{language_instruction}\n\n{JSON_SCHEMA_HINT}"

    try:
        if config["provider"] == "openai":
            raw_response = _call_openai_api(
                api_key=config["api_key"],
                model=config["model"],
                base_url=config["base_url"],
                prompt=prompt,
                image_bytes=image_bytes,
                mime_type=mime_type,
            )
        else:
            raw_response = _call_gemini_api(
                api_key=config["api_key"],
                model=config["model"],
                prompt=prompt,
                image_bytes=image_bytes,
                mime_type=mime_type,
            )

        clean_text = _clean_json_text(raw_response)
        parsed = json.loads(clean_text)

        # Standardize structured fields
        result = {
            "success": True,
            "configured": True,
            "crop": str(parsed.get("crop", "Unknown")),
            "possible_disease": str(parsed.get("possible_disease", "Indeterminate")),
            "confidence": str(parsed.get("confidence", "AI Estimate")),
            "symptoms": str(parsed.get("symptoms", "No specific symptoms reported.")),
            "possible_causes": str(parsed.get("possible_causes", "Uncertain causes.")),
            "immediate_steps": str(parsed.get("immediate_steps", "Isolate affected plants and consult an expert.")),
            "management": str(parsed.get("management", "Practice clean cultivation and sanitation.")),
            "prevention": str(parsed.get("prevention", "Use disease-resistant varieties and proper crop spacing.")),
            "expert_advice": str(parsed.get("expert_advice", "Consult your local agricultural extension officer before applying chemicals.")),
            "disclaimer": (
                "AI-assisted identification is an estimate based on the uploaded image. "
                "Consult a qualified agricultural expert or your local agriculture department before purchasing "
                "or applying any pesticides, fungicides, or chemical treatments."
            ),
        }
        return result

    except json.JSONDecodeError as err:
        logger.warning("Failed to parse AI JSON response: %s", err)
        return {
            "success": False,
            "configured": True,
            "error": AI_UNAVAILABLE_MESSAGE,
        }
    except requests.Timeout:
        logger.warning("AI API call timed out")
        return {
            "success": False,
            "configured": True,
            "error": AI_UNAVAILABLE_MESSAGE,
        }
    except Exception as err:
        logger.exception("AI leaf analysis encountered an error")
        safe_error = str(err).strip()
        if len(safe_error) > 240:
            safe_error = safe_error[:240] + "..."
        return {
            "success": False,
            "configured": True,
            "error": AI_UNAVAILABLE_MESSAGE,
        }


def chat_about_crop(
    user_message: str,
    previous_analysis: Optional[Dict[str, Any]] = None,
    chat_history: Optional[List[Dict[str, str]]] = None,
    language: str = "en",
) -> Dict[str, Any]:
    """
    Provides interactive conversational assistance about the analyzed leaf/crop.
    Remembers previous crop analysis in the session.
    """
    if not is_ai_configured():
        return {
            "success": False,
            "configured": False,
            "error": "AI analysis is not configured. Please configure the AI API key in the .env file.",
        }

    config = get_ai_config()

    context_prompt = (
        "You are an agricultural crop expert and farmer assistant on BusinessVehicleBooking platform. "
        "Answer the farmer's question in a respectful, helpful, and accessible manner. "
        "SAFETY GUIDELINES: Do not prescribe exact chemical doses without verified label recommendations. "
        "Always advise the farmer to check with a local agricultural officer or extension center before applying any chemical pesticides. "
    )

    if language == "te":
        context_prompt += "Respond in Telugu (తెలుగు) unless the user asks for another language.\n"
    elif language == "hi":
        context_prompt += "Respond in Hindi (हिन्दी) unless the user asks for another language.\n"
    else:
        context_prompt += "Respond in clear English unless the user asks for another language.\n"

    if previous_analysis and isinstance(previous_analysis, dict):
        context_prompt += (
            f"\nCURRENT LEAF ANALYSIS IN THIS SESSION:\n"
            f"- Crop: {previous_analysis.get('crop')}\n"
            f"- Possible Disease: {previous_analysis.get('possible_disease')}\n"
            f"- Confidence: {previous_analysis.get('confidence')}\n"
            f"- Symptoms: {previous_analysis.get('symptoms')}\n"
            f"- Causes: {previous_analysis.get('possible_causes')}\n"
            f"- Immediate Steps: {previous_analysis.get('immediate_steps')}\n"
            f"- Management: {previous_analysis.get('management')}\n"
            f"- Prevention: {previous_analysis.get('prevention')}\n"
            f"Use this context when the farmer asks about 'this disease', 'this crop', 'this leaf', or 'what to do next'.\n"
        )
    else:
        context_prompt += "\nNo leaf image has been analyzed yet in this session. Provide general agricultural guidance.\n"

    # Incorporate recent conversation history (up to last 6 messages)
    history_text = ""
    if chat_history:
        for msg in chat_history[-6:]:
            role = "Farmer" if msg.get("role") == "user" else "Assistant"
            content = msg.get("content", "")
            history_text += f"{role}: {content}\n"

    full_prompt = (
        f"{context_prompt}\n"
        f"CONVERSATION HISTORY:\n{history_text}\n"
        f"Farmer: {user_message}\n"
        f"Assistant:"
    )

    try:
        if config["provider"] == "openai":
            reply = _call_openai_api(
                api_key=config["api_key"],
                model=config["model"],
                base_url=config["base_url"],
                prompt=full_prompt,
            )
        else:
            reply = _call_gemini_api(
                api_key=config["api_key"],
                model=config["model"],
                prompt=full_prompt,
            )

        return {
            "success": True,
            "configured": True,
            "reply": reply.strip(),
        }

    except requests.Timeout:
        return {
            "success": False,
            "configured": True,
            "error": "The AI assistant request timed out. Please try again.",
        }
    except Exception as err:
        logger.exception("Farmer chatbot error")
        safe_error = str(err).strip()
        if len(safe_error) > 240:
            safe_error = safe_error[:240] + "..."
        return {
            "success": False,
            "configured": True,
            "error": (
                "The AI assistant could not respond right now. "
                + (safe_error if safe_error else "Please try again.")
            ),
        }
