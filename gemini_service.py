"""Server-side Gemini client. The API key stays on the server and never reaches the browser."""
import json
import os
import time

import requests

# Google retires older models for new API keys, so the model can be changed with GEMINI_MODEL.
MODEL = (os.environ.get('GEMINI_MODEL') or 'gemini-3.8-flash').strip()
API_URL = f'https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent'
# Optional thinking level for the model ("low", "high"). Unset uses the model's default,
# which measured faster than "low" for gemini-3.8-flash.
THINKING_LEVEL = (os.environ.get('GEMINI_THINKING') or 'off').strip().lower()
TIMEOUT_SECONDS = 180  # newer models can take 1.5+ minutes for a long trip


class GeminiError(Exception):
    """Raised with a message that is safe to show to the user."""


def get_api_key():
    return (os.environ.get('GEMINI_API_KEY') or '').strip()


def has_api_key():
    return bool(get_api_key())


def build_prompt(location, duration, budget, style):
    return f'''Create a highly detailed, premium, realistic travel plan for: "{location}".
Trip parameters:
- Duration: {duration} day(s).
- Budget: {budget} (Budget, Standard, or Luxury).
- Travel Style: {style} (e.g. Culture, Adventure, Relaxation, Foodie, Urban & Landmarks).

Please make sure:
1. The description is compelling and welcoming.
2. The hotels match the budget category:
   - Budget: Cheap, clean, hostels or guesthouses.
   - Standard: Mid-range, highly rated, modern boutique hotels.
   - Luxury: Top-tier 5-star hotels, palaces, or luxury resorts.
3. The itinerary has a logical flow for each day, with a morning slot, a midday slot, and an evening slot.
4. Nearby scenic spots should include at least 4-5 major tourist attractions, viewpoints, or historical sites.
5. Provide a sequence of realistic routes between key points with estimated transit times and transportation modes.
6. If the travel style is "Adventure", explicitly include treks, hikes, visits to scenic waterfalls, and highlight the specific "base villages" where these journeys start. Detail these treks in the "treks" array with name, difficulty (e.g. Easy, Moderate, Challenging), duration, base_village, and description.
7. If the travel style is NOT "Adventure", return an empty array for "treks".
8. Provide realistic geographic latitude and longitude coordinates (decimal numbers) for each hotel, attraction, and schedule stop. Ensure these coordinates match the actual real-world location (e.g., Amer Fort is around 26.9855, 75.8513).
9. Keep every description, highlight and activity to one or two short sentences.
10. Return structured data exactly conforming to the JSON schema.'''


def _obj(properties, required):
    return {'type': 'OBJECT', 'properties': properties, 'required': required}


_S = {'type': 'STRING'}
_I = {'type': 'INTEGER'}
_N = {'type': 'NUMBER'}

PLAN_SCHEMA = _obj(
    {
        'destination': _S,
        'description': _S,
        'duration': _I,
        'budget': _S,
        'style': _S,
        'overview': _obj(
            {'daily_cost': _S, 'weather': _S, 'tips': _S},
            ['daily_cost', 'weather', 'tips'],
        ),
        'hotels': {'type': 'ARRAY', 'items': _obj(
            {'name': _S, 'price': _S, 'rating': _S, 'address': _S, 'highlight': _S,
             'latitude': _N, 'longitude': _N},
            ['name', 'price', 'rating', 'address', 'highlight', 'latitude', 'longitude'],
        )},
        'spots': {'type': 'ARRAY', 'items': _obj(
            {'name': _S, 'category': _S, 'rating': _S, 'highlight': _S,
             'latitude': _N, 'longitude': _N},
            ['name', 'category', 'rating', 'highlight', 'latitude', 'longitude'],
        )},
        'itinerary': {'type': 'ARRAY', 'items': _obj(
            {
                'day': _I,
                'schedule': {'type': 'ARRAY', 'items': _obj(
                    {'time': _S, 'spot': _S, 'activity': _S, 'cost': _S, 'duration': _S,
                     'transport': _S, 'latitude': _N, 'longitude': _N},
                    ['time', 'spot', 'activity', 'cost', 'duration', 'transport',
                     'latitude', 'longitude'],
                )},
            },
            ['day', 'schedule'],
        )},
        'routes': {'type': 'ARRAY', 'items': _obj(
            {'from': _S, 'to': _S, 'duration': _S, 'distance': _S,
             'modes': {'type': 'ARRAY', 'items': _S}},
            ['from', 'to', 'duration', 'distance', 'modes'],
        )},
        'treks': {'type': 'ARRAY', 'items': _obj(
            {'name': _S, 'difficulty': _S, 'duration': _S, 'base_village': _S, 'description': _S},
            ['name', 'difficulty', 'duration', 'description'],
        )},
    },
    ['destination', 'description', 'duration', 'budget', 'style',
     'overview', 'hotels', 'spots', 'itinerary', 'routes', 'treks'],
)


def generate_plan(location, duration, budget, style):
    api_key = get_api_key()
    if not api_key:
        raise GeminiError('Gemini API key is not configured on the server.')

    body = {
        'contents': [{'parts': [{'text': build_prompt(location, duration, budget, style)}]}],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'responseSchema': PLAN_SCHEMA,
        },
    }
    if THINKING_LEVEL != 'off':
        body['generationConfig']['thinkingConfig'] = {'thinkingLevel': THINKING_LEVEL}

    def send():
        try:
            return requests.post(
                API_URL,
                json=body,
                headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key},
                timeout=TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            raise GeminiError('Could not reach the Gemini API. Please try again.')

    response = send()
    # Gemini sometimes answers "high demand" (503) or rate-limited (429); one quick retry
    # usually gets through.
    if response.status_code in (429, 503):
        time.sleep(4)
        response = send()
    # A model without the thinking setting rejects it; ask again without it.
    if response.status_code == 400 and 'thinking' in response.text.lower():
        body['generationConfig'].pop('thinkingConfig', None)
        response = send()

    if not response.ok:
        message = 'Failed to communicate with Gemini API.'
        try:
            err = response.json().get('error') or {}
            if err.get('message'):
                message = err['message']
        except (ValueError, AttributeError):
            pass
        raise GeminiError(message)

    try:
        data = response.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
        return json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError):
        raise GeminiError('The AI returned a malformed itinerary. Please try again.')
