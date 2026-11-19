import asyncio
import re
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_URL = "https://services-api.ryanair.com/mcp-server/mcp"

OUTPUT_FILE = Path(
    "Ryanair_TRN_BGY_CPH_verificerede_afgange.ics"
)

ROUTES = [
    ("TRN", "CPH", "Torino"),
    ("BGY", "CPH", "Bergamo"),
]

DAYS_AHEAD = 365

ROME = ZoneInfo("Europe/Rome")
COPENHAGEN = ZoneInfo("Europe/Copenhagen")


def escape_ics(value):
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def extract_text(result):
    parts = []

    for item in result.content:
        if hasattr(item, "text"):
            parts.append(item.text)

    return "\n".join(parts)


def parse_flights(text, flight_date, origin, destination):
    """
    Parse Ryanair MCP search_flights output.

    The parser deliberately only accepts flights for which
    flight number, departure time and arrival time are present.
    This prevents an incomplete API response from generating
    false calendar entries.
    """

    flights = []

    # Ryanair flight numbers such as FR5089.
    flight_numbers = re.findall(
        r"\\b(FR\\s?\\d{1,5})\\b",
        text,
        flags=re.IGNORECASE,
    )

    # HH:MM times.
    times = re.findall(
        r"\\b([01]\\d|2[0-3]):([0-5]\\d)\\b",
        text,
    )

    time_values = [
        f"{hour}:{minute}"
        for hour, minute in times
    ]

    if not flight_numbers or len(time_values) < 2:
        return flights

    # Normally search_flights returns one card/block per flight.
    # Split output around each flight number to keep its times together.
    blocks = re.split(
        r"(?=\\bFR\\s?\\d{1,5}\\b)",
        text,
        flags=re.IGNORECASE,
    )

    for block in blocks:
        number_match = re.search(
            r"\\b(FR\\s?\\d{1,5})\\b",
            block,
            flags=re.IGNORECASE,
        )

        if not number_match:
            continue

        block_times = re.findall(
            r"\\b([01]\\d|2[0-3]):([0-5]\\d)\\b",
            block,
        )

        if len(block_times) < 2:
            continue

        flight_number = (
            number_match.group(1)
            .replace(" ", "")
            .upper()
        )

        departure_time = (
            f"{block_times[0][0]}:{block_times[0][1]}"
        )

        arrival_time = (
            f"{block_times[1][0]}:{block_times[1][1]}"
        )

        departure = datetime.combine(
            flight_date,
            datetime.strptime(
                departure_time,
                "%H:%M",
            ).time(),
            tzinfo=ROME,
        )

        arrival = datetime.combine(
            flight_date,
            datetime.strptime(
                arrival_time,
                "%H:%M",
            ).time(),
            tzinfo=COPENHAGEN,
        )

        # Flights arriving after midnight.
        if arrival <= departure.astimezone(COPENHAGEN):
            arrival += timedelta(days=1)

        flights.append(
            {
                "origin": origin,
                "destination": destination,
                "flight": flight_number,
                "departure": departure,
                "arrival": arrival,
            }
        )

    return flights


async def search_day(
    session,
    origin,
    destination,
    flight_date,
):
    result = await session.call_tool(
        "ryanair.search_flights",
        arguments={
            "origin": origin,
            "destination": destination,
            "dateOut": flight_date.isoformat(),
            "roundTrip": "false",
            "passengers": {
                "adults": 1,
                "teens": 0,
                "children": 0,
                "infants": 0,
            },
            "langCode": "en-IE",
        },
    )

    text = extract_text(result)

    return parse_flights(
        text,
        flight_date,
        origin,
        destination,
    )


def build_ics(flights):
    now = datetime.now(timezone.utc)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//hfedder//Ryanair Piemonte Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Ryanair Piemonte - CPH",
    ]

    for flight in sorted(
        flights,
        key=lambda x: x["departure"],
    ):
        departure = flight["departure"]
        arrival = flight["arrival"]

        # UID deliberately excludes the departure TIME.
        # If Ryanair changes a time, Outlook therefore updates
        # the existing event rather than creating a duplicate.
        uid = (
            f'{flight["flight"]}-'
            f'{flight["origin"]}-'
            f'{flight["destination"]}-'
            f'{departure:%Y%m%d}'
            "@hfedder-ryanair-calendar"
        )

        summary = (
            f'✈ {flight["flight"]} '
            f'{flight["origin"]} → '
            f'{flight["destination"]}'
        )

        description = (
            "Ryanair schedule automatically updated "
            "from Ryanair flight data."
        )

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{uid}",
                f"DTSTAMP:{now:%Y%m%dT%H%M%SZ}",
                (
                    "DTSTART;TZID=Europe/Rome:"
                    f"{departure:%Y%m%dT%H%M%S}"
                ),
                (
                    "DTEND;TZID=Europe/Copenhagen:"
                    f"{arrival:%Y%m%dT%H%M%S}"
                ),
                f"SUMMARY:{escape_ics(summary)}",
                (
                    "LOCATION:"
                    f'{flight["origin"]} → '
                    f'{flight["destination"]}'
                ),
                f"DESCRIPTION:{escape_ics(description)}",
                "STATUS:CONFIRMED",
                "TRANSP:TRANSPARENT",
                "END:VEVENT",
            ]
        )

    lines.append("END:VCALENDAR")

    return "\r\n".join(lines) + "\r\n"


async def main():
    today = date.today()
    until = today + timedelta(days=DAYS_AHEAD)

    all_flights = []

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

            current = today

            while current <= until:
                for origin, destination, city in ROUTES:
                    try:
                        flights = await search_day(
                            session,
                            origin,
                            destination,
                            current,
                        )

                        if flights:
                            print(
                                current,
                                city,
                                len(flights),
                                "flight(s)",
                            )

                            all_flights.extend(flights)

                    except Exception as exc:
                        print(
                            "WARNING:",
                            current,
                            origin,
                            str(exc),
                        )

                current += timedelta(days=1)

    # Safety gate.
    # Never overwrite Henrik's working calendar if Ryanair
    # unexpectedly returns too little data.
    if len(all_flights) < 20:
        raise RuntimeError(
            "Safety stop: Ryanair returned only "
            f"{len(all_flights)} flights. "
            "Existing ICS file has NOT been replaced."
        )

    # Remove accidental duplicates.
    unique = {}

    for flight in all_flights:
        key = (
            flight["flight"],
            flight["origin"],
            flight["destination"],
            flight["departure"],
        )

        unique[key] = flight

    flights = list(unique.values())

    ics = build_ics(flights)

    OUTPUT_FILE.write_text(
        ics,
        encoding="utf-8",
        newline="",
    )

    print(
        f"Calendar updated with {len(flights)} flights."
    )


if __name__ == "__main__":
    asyncio.run(main())
