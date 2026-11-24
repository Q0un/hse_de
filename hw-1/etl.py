import logging
import math
import os
import re
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from dotenv import dotenv_values
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logger = logging.getLogger("population_etl")
INDICATOR = "SP.POP.TOTL"
TARGET_COLUMNS = [
    "country_code", "country_name", "year", "population",
    "indicator_code", "source_updated_at",
]


@dataclass(frozen=True)
class Config:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str = field(repr=False)
    api_base_url: str = "https://api.worldbank.org/v2"
    countries: tuple[str, ...] = ("BRA", "CHN", "IND", "ZAF", "RUS", "USA", "DEU", "FRA", "JPN", "GBR")
    start_year: int = 2000
    end_year: int = 2024
    page_size: int = 50
    http_timeout: float = 30.0


def read_config(project_root: Path) -> Config:
    env_path = project_root / ".env"
    if not env_path.is_file():
        raise FileNotFoundError("Нет .env. Выполните bash run.sh из корня проекта.")
    settings = {**dotenv_values(env_path), **os.environ}
    required = ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    missing = [name for name in required if not settings.get(name)]
    if missing:
        raise ValueError(f"Не заданы параметры: {', '.join(missing)}")
    if settings["POSTGRES_PASSWORD"] == "replace_with_your_local_password":
        raise ValueError("В .env остался шаблонный пароль. Выполните bash run.sh из корня проекта.")
    try:
        config = Config(
            db_host=settings["POSTGRES_HOST"],
            db_port=int(settings["POSTGRES_PORT"]),
            db_name=settings["POSTGRES_DB"],
            db_user=settings["POSTGRES_USER"],
            db_password=settings["POSTGRES_PASSWORD"],
            api_base_url=settings.get("WB_API_BASE_URL", "https://api.worldbank.org/v2").rstrip("/"),
            countries=tuple(code.strip().upper() for code in settings.get("WB_COUNTRIES", "BRA,CHN,IND,ZAF,RUS,USA,DEU,FRA,JPN,GBR").split(",")),
            start_year=int(settings.get("WB_START_YEAR", "2000")),
            end_year=int(settings.get("WB_END_YEAR", "2024")),
            page_size=int(settings.get("WB_PAGE_SIZE", "50")),
            http_timeout=float(settings.get("HTTP_TIMEOUT_SECONDS", "30")),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Проверьте числовые параметры в .env.") from exc
    if not 1 <= config.db_port <= 65535:
        raise ValueError("POSTGRES_PORT должен быть в диапазоне 1–65535.")
    if not config.countries or any(not re.fullmatch(r"[A-Z]{3}", code) for code in config.countries):
        raise ValueError("WB_COUNTRIES должен содержать ISO3-коды через запятую.")
    if len(set(config.countries)) != len(config.countries):
        raise ValueError("В WB_COUNTRIES повторяются страны.")
    if not 1900 <= config.start_year <= config.end_year <= 2100:
        raise ValueError("Нужен диапазон 1900 <= WB_START_YEAR <= WB_END_YEAR <= 2100.")
    if not 1 <= config.page_size <= 1000:
        raise ValueError("WB_PAGE_SIZE должен быть в диапазоне 1–1000.")
    if not math.isfinite(config.http_timeout) or config.http_timeout <= 0:
        raise ValueError("HTTP_TIMEOUT_SECONDS должен быть положительным конечным числом.")
    if not config.api_base_url.startswith("https://"):
        raise ValueError("WB_API_BASE_URL должен использовать HTTPS.")
    return config


def create_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    session.headers["User-Agent"] = "HSE-DE-HW1/1.0"
    return session


def extract_population(config: Config, session: requests.Session) -> tuple[list[dict], date]:
    url = f"{config.api_base_url}/country/{';'.join(config.countries)}/indicator/{INDICATOR}"
    records = []
    page = 1
    first_metadata = None
    while True:
        try:
            response = session.get(
                url,
                params={
                    "format": "json", "source": 2,
                    "date": f"{config.start_year}:{config.end_year}",
                    "per_page": config.page_size, "page": page,
                },
                timeout=config.http_timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.error("Не удалось получить страницу %s API Всемирного банка.", page)
            raise RuntimeError(f"Ошибка API на странице {page}: проверьте интернет и доступность источника.") from exc
        if (
            not isinstance(payload, list) or len(payload) != 2
            or not isinstance(payload[0], dict) or not isinstance(payload[1], list)
        ):
            raise ValueError(f"API вернул ошибку или неожиданный JSON на странице {page}.")
        metadata, page_records = payload
        try:
            current_page = int(metadata["page"])
            pages = int(metadata["pages"])
            total = int(metadata["total"])
            updated_at = date.fromisoformat(metadata["lastupdated"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Повреждены метаданные API на странице {page}.") from exc
        if current_page != page or not 1 <= page <= pages <= 10000 or total <= 0:
            raise ValueError(f"Некорректная пагинация API на странице {page}.")
        if not page_records or not all(isinstance(record, dict) for record in page_records):
            raise ValueError(f"Пустая или повреждённая страница {page}; загрузка отменена.")
        version = (pages, total, updated_at)
        if first_metadata is None:
            first_metadata = version
        elif version != first_metadata:
            raise ValueError("Источник изменился во время пагинации. Повторите Extract.")
        records.extend(page_records)
        logger.info("Extract: страница %s/%s, строк %s.", page, pages, len(page_records))
        if page == pages:
            break
        page += 1
    if len(records) != total:
        raise ValueError(f"Получено {len(records)} строк вместо {total}; загрузка отменена.")
    logger.info("Extract завершён: %s строк, версия источника %s.", len(records), updated_at)
    return records, updated_at


def transform_population(records: list[dict], updated_at: date, config: Config) -> pd.DataFrame:
    if not records:
        raise ValueError("Extract не вернул данные; пустая загрузка запрещена.")
    frame = pd.json_normalize(records)
    mapping = {
        "countryiso3code": "country_code", "country.value": "country_name",
        "date": "year", "value": "population", "indicator.id": "indicator_code",
    }
    missing = set(mapping).difference(frame.columns)
    if missing:
        raise ValueError(f"В ответе API нет полей: {', '.join(sorted(missing))}")
    frame = frame[list(mapping)].rename(columns=mapping)
    missing_values = int(frame["population"].isna().sum())
    frame = frame.dropna(subset=["population"]).copy()
    if frame.empty:
        raise ValueError("У всех наблюдений отсутствует население; загрузка отменена.")
    frame["country_code"] = frame["country_code"].astype("string").str.strip().str.upper()
    frame["country_name"] = frame["country_name"].astype("string").str.strip()
    if not frame["country_code"].isin(config.countries).all():
        raise ValueError("API вернул отсутствующий или незапрошенный код страны.")
    if frame["country_name"].isna().any() or frame["country_name"].eq("").any():
        raise ValueError("В данных отсутствует название страны.")
    if not frame["indicator_code"].eq(INDICATOR).all():
        raise ValueError("В ответе API есть другой показатель.")
    for column, lower, upper in [
        ("year", config.start_year, config.end_year),
        ("population", 0, 2**63 - 1),
    ]:
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if not (numeric.between(lower, upper) & numeric.mod(1).eq(0)).all():
            raise ValueError(f"Поле {column} содержит некорректные значения.")
        integer_values = [int(value) for value in numeric]
        if any(value < lower or value > upper for value in integer_values):
            raise ValueError(f"Поле {column} выходит за допустимый диапазон.")
        frame[column] = pd.Series(integer_values, index=frame.index, dtype="int64")
    before_deduplication = len(frame)
    frame = frame.drop_duplicates()
    if frame.duplicated(["country_code", "year"]).any():
        raise ValueError("Для одной страны и года получены противоречивые записи.")
    frame["source_updated_at"] = updated_at
    frame = frame[TARGET_COLUMNS].sort_values(["country_code", "year"]).reset_index(drop=True)
    logger.info(
        "Transform: %s -> %s строк; пропусков населения %s, точных дублей %s.",
        len(records), len(frame), missing_values, before_deduplication - len(frame),
    )
    return frame


def connect_to_postgres(config: Config):
    import psycopg2

    try:
        return psycopg2.connect(
            host=config.db_host, port=config.db_port, dbname=config.db_name,
            user=config.db_user, password=config.db_password, connect_timeout=10,
        )
    except psycopg2.Error as exc:
        logger.error("Не удалось подключиться к PostgreSQL %s:%s.", config.db_host, config.db_port)
        raise RuntimeError("PostgreSQL недоступен. Проверьте .env и docker compose ps.") from exc


def load_population(frame: pd.DataFrame, config: Config) -> int:
    if frame.empty:
        raise ValueError("Загрузка пустого набора запрещена.")
    rows = list(frame[TARGET_COLUMNS].itertuples(index=False, name=None))
    statement = """
        INSERT INTO stg.population AS target
            (country_code, country_name, year, population, indicator_code, source_updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (country_code, year) DO UPDATE SET
            country_name = EXCLUDED.country_name,
            population = EXCLUDED.population,
            indicator_code = EXCLUDED.indicator_code,
            source_updated_at = EXCLUDED.source_updated_at,
            loaded_at = CURRENT_TIMESTAMP
        WHERE (target.country_name, target.population, target.indicator_code, target.source_updated_at)
            IS DISTINCT FROM
            (EXCLUDED.country_name, EXCLUDED.population, EXCLUDED.indicator_code, EXCLUDED.source_updated_at)
    """
    try:
        with closing(connect_to_postgres(config)) as connection:
            with connection:
                with connection.cursor() as cursor:
                    cursor.executemany(statement, rows)
    except Exception as exc:
        logger.error("Load не выполнен: транзакция отменена.")
        raise RuntimeError("Не удалось загрузить данные. Проверьте PostgreSQL и init/init.sql.") from exc
    logger.info("Load завершён: обработано %s строк в stg.population.", len(rows))
    return len(rows)
