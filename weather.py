"""
Прогноз погоды через бесплатный сервис Open-Meteo (https://open-meteo.com).
Регистрация и ключ не нужны.
"""

import html

import httpx

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Порог "сильного ветра", м/с
STRONG_WIND = 10
STRONG_GUSTS = 15

# Коды погоды (стандарт WMO) -> описание
WEATHER_CODES = {
    0: "Ясно, без осадков",
    1: "Преимущественно ясно, без осадков",
    2: "Переменная облачность, без осадков",
    3: "Облачно, без осадков",
    45: "Туман",
    48: "Туман с изморозью",
    51: "Небольшая морось",
    53: "Морось",
    55: "Сильная морось",
    56: "Ледяная морось",
    57: "Сильная ледяная морось",
    61: "Небольшой дождь",
    63: "Дождь",
    65: "Сильный дождь",
    66: "Ледяной дождь",
    67: "Сильный ледяной дождь",
    71: "Небольшой снег",
    73: "Снег",
    75: "Сильный снег",
    77: "Снежная крупа",
    80: "Кратковременный дождь",
    81: "Ливень",
    82: "Сильный ливень",
    85: "Кратковременный снег",
    86: "Сильный снегопад",
    95: "Гроза",
    96: "Гроза с градом",
    99: "Сильная гроза с градом",
}

# Какой час брать для "утра", "дня" и "вечера"
PARTS_OF_DAY = [("Утро", 8), ("День", 14), ("Вечер", 20)]


class WeatherError(Exception):
    """Не удалось получить данные о погоде (нет интернета, сервис недоступен и т.п.)."""


async def _get(url: str, params: dict) -> dict:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise WeatherError(str(exc)) from exc


async def find_cities(name: str, count: int = 5) -> list:
    """
    Ищет город по названию. Возвращает список словарей:
    {"name", "lat", "lon", "label"}, где label — "Москва, Москва, Россия".
    """
    data = await _get(GEOCODING_URL, {"name": name, "count": count, "language": "ru", "format": "json"})
    return [{
        "name": item["name"],
        "lat": item["latitude"],
        "lon": item["longitude"],
        # Без повторов и пустых частей: "Москва, Москва, Россия" -> "Москва, Россия"
        "label": ", ".join(dict.fromkeys(p for p in (item.get("name"), item.get("admin1"), item.get("country")) if p)),
    } for item in data.get("results") or []]


def _fmt_temp(value) -> str:
    if value is None:
        return "—"
    t = round(value)
    return f"+{t}°" if t > 0 else f"{t}°"


def format_forecast(city_name: str, data: dict) -> str:
    """Превращает ответ сервиса в короткое сообщение."""
    daily = data["daily"]
    code = daily["weather_code"][0]
    condition = WEATHER_CODES.get(code, "Без особых явлений")

    hours = data["hourly"]["time"]          # ["2026-10-28T00:00", "2026-10-28T01:00", ...]
    temps = data["hourly"]["temperature_2m"]
    by_hour = {int(h[11:13]): t for h, t in zip(hours, temps)}

    lines = [f"<b>ПОГОДА · {html.escape(city_name.upper())}</b>", "<i>Сегодня</i>", "", condition]

    wind = daily["wind_speed_10m_max"][0] or 0
    gusts = daily["wind_gusts_10m_max"][0] or 0
    if wind >= STRONG_WIND or gusts >= STRONG_GUSTS:
        lines.append(f"Сильный ветер — до {round(wind)} м/с, порывы до {round(gusts)} м/с")

    # Моноширинный блок, чтобы температуры стояли ровным столбиком
    rows = [f"{label:<7}{_fmt_temp(by_hour.get(hour)):>5}" for label, hour in PARTS_OF_DAY]
    lines.append("<pre>" + "\n".join(rows) + "</pre>")
    return "\n".join(lines)


async def today_text(city_name: str, lat: float, lon: float) -> str:
    """Загружает прогноз на сегодня (по местному времени города) и готовит сообщение."""
    data = await _get(FORECAST_URL, {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "daily": "weather_code,wind_speed_10m_max,wind_gusts_10m_max",
        "wind_speed_unit": "ms",
        "timezone": "auto",
        "forecast_days": 1,
    })
    return format_forecast(city_name, data)
