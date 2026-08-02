import csv
import dbcon

db = dbcon.SESSION_LOCAL()

count = 0

with open("airports.csv", "r", encoding="utf-8") as file:
    reader = csv.DictReader(file)

    for row in reader:

        # Skip airports without an IATA code
        if not row["iata_code"]:
            continue

        # Skip duplicates
        exists = db.query(dbcon.Airport).filter(
            dbcon.Airport.iata_code == row["iata_code"]
        ).first()

        if exists:
            continue

        airport = dbcon.Airport(
            iata_code=row["iata_code"],
            icao_code=row["gps_code"],
            airport_name=row["name"],
            city=row["municipality"] or "",
            country=row["iso_country"],
            latitude=float(row["latitude_deg"]),
            longitude=float(row["longitude_deg"])
        )

        db.add(airport)
        count += 1

db.commit()
db.close()

print(f"Imported {count} airports successfully!")