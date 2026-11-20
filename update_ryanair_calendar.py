import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_URL = "https://services-api.ryanair.com/mcp-server/mcp"

OUTPUT_FILE = Path(
    "Ryanair_TRN_BGY_CPH_verificerede_afgange.ics"
)

# Alle fire retninger.
ROUTES = [
    ("TRN", "CPH"),
    ("BGY", "CPH"),
    ("CPH", "TRN"),
    ("CPH", "BGY"),
]

# Rullende periode.
DAYS_AHEAD = 365

# Antal parallelle forespørgsler.
# Holdes relativt lavt for ikke at overbelaste Ryanair MCP.
MAX_CONCURRENT_REQUESTS = 6

# Der skal findes mindst dette antal fly, før den eksisterende
# Outlook-kalender må overskrives.
MINIMUM_FLIGHTS = 20

ITALY_TIMEZONE = ZoneInfo("Europe/Rome")
DENMARK_TIMEZONE = ZoneInfo("Europe/Copenhagen")


def airport_timezone(airport_code):
    if airport_code == "CPH":
        return DENMARK_TIMEZONE

    return ITALY_TIMEZONE


def escape_ics(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold_ics_line(line):
    """
    Fold lange ICS-linjer efter iCalendar-princippet.
    Fortsættelseslinjer starter med ét mellemrum.
    """

    encoded = line.encode("utf-8")
    parts = []

    while len(encoded) > 73:
        cut = 73

        while cut > 0 and encoded[cut] & 0xC0 == 0x80:
            cut -= 1

        parts.append(encoded[:cut].decode("utf-8"))
        encoded = encoded[cut:]

    parts.append(encoded.decode("utf-8"))

    return "\r\n ".join(parts)


def extract_result_text(result):
    texts = []

    for item in result.content:
        if hasattr(item, "text") and item.text:
            texts.append(item.text)

    return "\n".join(texts)


def parse_flights(
    text,
    flight_date,
    expected_origin,
    expected_destination,
):
    """
    Parser Ryanairs tekstlinjer, eksempelvis:

    FR5089: TRN 09:20 → CPH 11:30 | 66.44 EUR

    Datoen kommer fra den konkrete dagsforespørgsel.
    """

    pattern = re.compile(
        r"\b(FR\s?\d{1,5})\s*:\s*"
        r"([A-Z]{3})\s+"
        r"([0-2]\d:[0-5]\d)\s*"
        r"(?:→|->|-)\s*"
        r"([A-Z]{3})\s+"
        r"([0-2]\d:[0-5]\d)"
        r"(?:\s*\|\s*([0-9]+(?:[.,][0-9]+)?)\s*([A-Z]{3}))?",
        flags=re.IGNORECASE,
    )

    flights = []

    for match in pattern.finditer(text):
        flight_number = (
            match.group(1)
            .replace(" ", "")
            .upper()
        )

        origin = match.group(2).upper()
        departure_text = match.group(3)
        destination = match.group(4).upper()
        arrival_text = match.group(5)

        price_text = match.group(6)
        currency = match.group(7)

        if origin != expected_origin:
            continue

        if destination != expected_destination:
            continue

        departure_timezone = airport_timezone(origin)
        arrival_timezone = airport_timezone(destination)

        departure_time = datetime.strptime(
            departure_text,
            "%H:%M",
        ).time()

        arrival_time = datetime.strptime(
            arrival_text,
            "%H:%M",
        ).time()

        departure = datetime.combine(
            flight_date,
            departure_time,
            tzinfo=departure_timezone,
        )

        arrival = datetime.combine(
            flight_date,
            arrival_time,
            tzinfo=arrival_timezone,
        )

        # Sammenlign i UTC for at afgøre, om ankomsten er
        # efter midnat lokal tid.
        if (
            arrival.astimezone(timezone.utc)
            <= departure.astimezone(timezone.utc)
        ):
            arrival += timedelta(days=1)

        price = None

        if price_text:
            price = price_text.replace(",", ".")

        flights.append(
            {
                "flight_number": flight_number,
                "origin": origin,
                "destination": destination,
                "departure": departure,
                "arrival": arrival,
                "price": price,
                "currency": currency,
            }
        )

    return flights


async def search_route_day(
    session,
    semaphore,
    origin,
    destination,
    flight_date,
):
    """
    Henter afgange for én rute på én bestemt dato.
    """

    async with semaphore:
        arguments = {
            "outboundDepartureDateFrom": (
                flight_date.isoformat()
            ),
            "outboundDepartureDateTo": (
                flight_date.isoformat()
            ),
            "departureIataCodes": [origin],
            "arrivalIataCodes": [destination],
            "currency": "EUR",
            "limit": 50,
            "pageNumber": 0,
        }

        # Ét ekstra forsøg ved midlertidig MCP/Ryanair-fejl.
        for attempt in range(2):
            try:
                result = await session.call_tool(
                    "pricing-get-cheapest-one-way-flight",
                    arguments=arguments,
                )

                text = extract_result_text(result)

                # Tool-fejl kan returneres som tekst.
                lower_text = text.lower()

                if (
                    "failed to fetch" in lower_text
                    or "bad request" in lower_text
                    or "tool not found" in lower_text
                ):
                    raise RuntimeError(text)

                flights = parse_flights(
                    text,
                    flight_date,
                    origin,
                    destination,
                )

                return {
                    "flights": flights,
                    "error": None,
                }

            except Exception as exc:
                if attempt == 0:
                    await asyncio.sleep(2)
                    continue

                return {
                    "flights": [],
                    "error": (
                        f"{flight_date} "
                        f"{origin}->{destination}: {exc}"
                    ),
                }

def make_uid(flight):
    departure_date = flight["departure"].strftime("%Y%m%d")

    flight_number = flight["flight_number"]
    origin = flight["origin"]
    destination = flight["destination"]

    return (
        flight_number
        + "-"
        + origin
        + "-"
        + destination
        + "-"
        + departure_date
        + "@hfedder-ryanair-calendar"
    )

def build_description(flight):
    parts = [
        "Ryanair-afgang automatisk hentet via "
        "Ryanairs offentlige MCP-tjeneste.",
        (
            f'Rute: {flight["origin"]} '
            f'→ {flight["destination"]}.'
        ),
    ]

    if flight["price"] and flight["currency"]:
        parts.append(
            "Observeret pris ved seneste opdatering: "
            f'{flight["price"]} {flight["currency"]}.'
        )

    parts.append(
        "Pris og flyvetid kan ændres. Kontrollér altid "
        "den konkrete afgang hos Ryanair før booking."
    )

    return " ".join(parts)


def build_ics(flights):
    generated = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        (
            "PRODID:-//hfedder//"
            "Ryanair Piemonte Calendar//DA"
        ),
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Ryanair Piemonte - CPH",
        "X-WR-CALDESC:Ryanair afgange mellem København og Piemonte",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    for flight in sorted(
        flights,
        key=lambda item: (
            item["departure"],
            item["origin"],
            item["flight_number"],
        ),
    ):
        departure = flight["departure"]
        arrival = flight["arrival"]

        departure_timezone_name = (
            "Europe/Copenhagen"
            if flight["origin"] == "CPH"
            else "Europe/Rome"
        )

        arrival_timezone_name = (
            "Europe/Copenhagen"
            if flight["destination"] == "CPH"
            else "Europe/Rome"
        )

        summary = (
            f'✈ {flight["flight_number"]} '

            f'{flight["origin"]} → '
            f'{flight["destination"]}'
        )

        location = (
            f'{flight["origin"]} → '
            f'{flight["destination]}'
        )

        description = build_description(flight)

        event_lines = [
            "BEGIN:VEVENT",
            f"UID:{make_uid(flight)}",
            f"DTSTAMP:{generated:%Y%m%dT%H%M%SZ}",
            (
                f"LAST-MODIFIED:"
                f"{generated:%Y%m%dT%H%M%SZ}"
            ),
            (
                f"DTSTART;TZID={departure_timezone_name}:"
                f"{departure:%Y%m%dT%H%M%S}"
            ),
            (
                f"DTEND;TZID={arrival_timezone_name}:"
                f"{arrival:%Y%m%dT%H%M%S}"
            ),
            f"SUMMARY:{escape_ics(summary)}",
            f"LOCATION:{escape_ics(location)}",
            f"DESCRIPTION:{escape_ics(description)}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]

        lines.extend(event_lines)

    lines.append("END:VCALENDAR")

    return (
        "\r\n".join(
            fold_ics_line(line)
            for line in lines
        )
        + "\r\n"
    )


def remove_duplicates(flights):
    """
    Fjerner dubletter, men bevarer separate afgange samme dag,
    hvis flynummeret er forskelligt.
    """

    unique = {}

    for flight in flights:
        key = (
            flight["flight_number"],
            flight["origin"],
            flight["destination"],
            flight["departure"].date(),
        )

        unique[key] = flight

    return list(unique.values())


async def main():
    today = date.today()
    last_date = today + timedelta(days=DAYS_AHEAD)

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_REQUESTS
    )

    all_flights = []
    errors = []

    async with streamable_http_client(
        MCP_URL
    ) as (
        read_stream,
        write_stream,
    ):
        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:
            await session.initialize()

            tasks = []

            current_date = today

            while current_date <= last_date:
                for origin, destination in ROUTES:
                    tasks.append(
                        search_route_day(
                            session,
                            semaphore,
                            origin,
                            destination,
                            current_date,
                        )
                    )

                current_date += timedelta(days=1)

            print(
                f"Searching {len(tasks)} route/date combinations"
            )

            # Kør i grupper for at holde memory og forbindelsen stabil.
            batch_size = 40

            for start in range(0, len(tasks), batch_size):
                batch = tasks[
                    start:start + batch_size
                ]

                batch_results = await asyncio.gather(
                    *batch
                )

                for result in batch_results:
                    all_flights.extend(
                        result["flights"]
                    )

                    if result["error"]:
                        errors.append(
                            result["error"]
                        )

                print(
                    f"Completed "
                    f"{min(start + batch_size, len(tasks))}"
                    f"/{len(tasks)} searches"
                )

    flights = remove_duplicates(all_flights)

    print(
        f"Found {len(flights)} unique flights"
    )

    route_counts = {}

    for origin, destination in ROUTES:
        count = sum(
            1
            for flight in flights
            if flight["origin"] == origin
            and flight["destination"] == destination
        )

        route_counts[
            f"{origin}->{destination}"
        ] = count

        print(
            f"{origin}->{destination}: "
            f"{count} flight(s)"
        )

    print(
        f"Request errors: {len(errors)}"
    )

    # Vis højst de første 20 fejl i GitHub-loggen.
    for error in errors[:20]:
        print(f"WARNING: {error}")

    # Sikkerhedsstop 1:
    # Overskriv aldrig den eksisterende kalender med for få fly.
    if len(flights) < MINIMUM_FLIGHTS:
        raise RuntimeError(
            "Safety stop: only "
            f"{len(flights)} flights were found. "
            "The existing ICS calendar was not replaced."
        )

    # Sikkerhedsstop 2:
    # Der skal være mindst én afgang i begge geografiske retninger.
    italy_to_denmark = sum(
        route_counts.get(route, 0)
        for route in (
            "TRN->CPH",
            "BGY->CPH",
        )
    )

    denmark_to_italy = sum(
        route_counts.get(route, 0)
        for route in (
            "CPH->TRN",
            "CPH->BGY",
        )
    )

    if italy_to_denmark == 0:
        raise RuntimeError(
            "Safety stop: no Italy-to-Denmark flights found. "
            "The existing ICS calendar was not replaced."
        )

    if denmark_to_italy == 0:
        raise RuntimeError(
            "Safety stop: no Denmark-to-Italy flights found. "
            "The existing ICS calendar was not replaced."
        )

    # Sikkerhedsstop 3:
    # Undgå overskrivning ved omfattende API-problemer.
    if len(errors) > 200:
        raise RuntimeError(
            "Safety stop: too many Ryanair request errors "
            f"({len(errors)}). "
            "The existing ICS calendar was not replaced."
        )

    ics_content = build_ics(flights)

    OUTPUT_FILE.write_text(
        ics_content,
        encoding="utf-8",
        newline="",
    )

    print(
        f"ICS calendar updated with "
        f"{len(flights)} flights."
    )


if __name__ == "__main__":
    asyncio.run(main())
