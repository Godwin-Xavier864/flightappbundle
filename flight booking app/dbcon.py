import uuid

from sqlalchemy import String,Column,Integer,create_engine,Float,ForeignKey,text,DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker,relationship


SQL_DB_URL= "sqlite:///./test.db"
engine= create_engine(SQL_DB_URL)
SESSION_LOCAL=sessionmaker(autocommit=False,autoflush=False,bind=engine)
Base=declarative_base()


def new_uuid():
    return str(uuid.uuid4())


def is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False



class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=new_uuid, index=True)
    username = Column(String(20), unique=True, index=True)
    email = Column(String(100), unique=True)
    hashed_password = Column(String(255))
    is_admin = Column(Integer, default=0, index=True)

    weather_history = relationship(
        "WeatherHistory",
        back_populates="user",
        cascade="all, delete"
    )


class WeatherHistory(Base):
    __tablename__ = "weather_history"

    id = Column(String(36), primary_key=True, default=new_uuid, index=True)

    city = Column(String(100))
    temperature = Column(Float)
    humidity = Column(Float)
    wind_speed = Column(Float)

    user_id = Column(String(36), ForeignKey("users.id"))

    user = relationship("User", back_populates="weather_history")
    
    
    
    
    
class Airport(Base):
    __tablename__ = "airports"

    id = Column(String(36), primary_key=True, default=new_uuid, index=True)

    iata_code = Column(String(3), unique=True, index=True)
    icao_code = Column(String(4))
    airport_name = Column(String(255))
    city = Column(String(100))
    country = Column(String(100))

    latitude = Column(Float)
    longitude = Column(Float)


class FlightSeat(Base):
    __tablename__ = "flight_seats"

    id = Column(String(36), primary_key=True, default=new_uuid, index=True)
    flight_instance_id = Column(String(120), unique=True, index=True)
    flight_number = Column(String(20), index=True)
    departure_time = Column(String(40), index=True)
    economy_available = Column(Integer, default=120)
    business_available = Column(Integer, default=20)
    economy_price = Column(Integer)
    business_price = Column(Integer)


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String(36), primary_key=True, default=new_uuid, index=True)
    flight_instance_id = Column(String(120), index=True)
    flight_number = Column(String(20), index=True)
    departure_time = Column(String(40), index=True)
    travel_class = Column(String(20))
    seats = Column(Integer)
    amount = Column(Integer)
    payment_order_id = Column(String(100), unique=True, index=True)
    idempotency_key = Column(String(100), unique=True, index=True)
    status = Column(String(20), default="pending", index=True)
    reservation_expires_at = Column(DateTime)
    refund_status = Column(String(20), default="none", index=True)
    refund_reason = Column(String(500))
    refund_requested_at = Column(DateTime)
    refund_admin_note = Column(String(500))
    refund_resolved_at = Column(DateTime)

    user_id = Column(String(36), ForeignKey("users.id"))


def add_column_if_missing(table_name, column_name, column_sql):
    with engine.begin() as connection:
        columns = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        existing_columns = {column[1] for column in columns}

        if column_name not in existing_columns:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"))


def add_index_if_missing(index_name, table_name, columns, unique=False):
    with engine.begin() as connection:
        indexes = connection.execute(text(f"PRAGMA index_list({table_name})")).fetchall()
        existing_indexes = {index[1] for index in indexes}

        if index_name not in existing_indexes:
            unique_sql = "UNIQUE " if unique else ""
            connection.execute(text(
                f"CREATE {unique_sql}INDEX {index_name} ON {table_name} ({columns})"
            ))


def table_exists(connection, table_name):
    return connection.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:table_name"),
        {"table_name": table_name}
    ).fetchone() is not None


def column_type(connection, table_name, column_name):
    columns = connection.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    for column in columns:
        if column[1] == column_name:
            return (column[2] or "").upper()
    return ""


def table_rows(connection, table_name):
    if not table_exists(connection, table_name):
        return []
    return [
        dict(row._mapping)
        for row in connection.execute(text(f"SELECT * FROM {table_name}")).fetchall()
    ]


def rebuild_table(connection, table_name, create_sql, insert_sql, rows):
    connection.execute(text(f"DROP TABLE IF EXISTS {table_name}_uuid"))
    connection.execute(text(create_sql))

    for row in rows:
        connection.execute(text(insert_sql), row)

    connection.execute(text(f"DROP TABLE {table_name}"))
    connection.execute(text(f"ALTER TABLE {table_name}_uuid RENAME TO {table_name}"))


def migrate_existing_integer_ids_to_uuid():
    with engine.begin() as connection:
        if not table_exists(connection, "users"):
            return

        if "INT" not in column_type(connection, "users", "id"):
            return

        connection.execute(text("PRAGMA foreign_keys=OFF"))

        users = table_rows(connection, "users")
        user_id_map = {}
        for user in users:
            old_id = str(user["id"])
            user_id_map[old_id] = old_id if is_uuid(old_id) else new_uuid()

        weather_rows = table_rows(connection, "weather_history")
        airport_rows = table_rows(connection, "airports")
        flight_seat_rows = table_rows(connection, "flight_seats")
        booking_rows = table_rows(connection, "bookings")

        user_rows = []
        for user in users:
            user_rows.append({
                "id": user_id_map[str(user["id"])],
                "username": user.get("username"),
                "email": user.get("email"),
                "hashed_password": user.get("hashed_password"),
                "is_admin": user.get("is_admin", 0),
            })

        weather_uuid_rows = []
        for row in weather_rows:
            weather_uuid_rows.append({
                "id": str(row["id"]) if is_uuid(row.get("id")) else new_uuid(),
                "city": row.get("city"),
                "temperature": row.get("temperature"),
                "humidity": row.get("humidity"),
                "wind_speed": row.get("wind_speed"),
                "user_id": user_id_map.get(str(row.get("user_id")), row.get("user_id")),
            })

        airport_uuid_rows = []
        for row in airport_rows:
            airport_uuid_rows.append({
                "id": str(row["id"]) if is_uuid(row.get("id")) else new_uuid(),
                "iata_code": row.get("iata_code"),
                "icao_code": row.get("icao_code"),
                "airport_name": row.get("airport_name"),
                "city": row.get("city"),
                "country": row.get("country"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
            })

        flight_seat_uuid_rows = []
        for row in flight_seat_rows:
            flight_seat_uuid_rows.append({
                "id": str(row["id"]) if is_uuid(row.get("id")) else new_uuid(),
                "flight_instance_id": row.get("flight_instance_id"),
                "flight_number": row.get("flight_number"),
                "departure_time": row.get("departure_time"),
                "economy_available": row.get("economy_available", 120),
                "business_available": row.get("business_available", 20),
                "economy_price": row.get("economy_price"),
                "business_price": row.get("business_price"),
            })

        booking_uuid_rows = []
        for row in booking_rows:
            booking_uuid_rows.append({
                "id": str(row["id"]) if is_uuid(row.get("id")) else new_uuid(),
                "flight_instance_id": row.get("flight_instance_id"),
                "flight_number": row.get("flight_number"),
                "departure_time": row.get("departure_time"),
                "travel_class": row.get("travel_class"),
                "seats": row.get("seats"),
                "amount": row.get("amount"),
                "payment_order_id": row.get("payment_order_id"),
                "idempotency_key": row.get("idempotency_key"),
                "status": row.get("status", "pending"),
                "reservation_expires_at": row.get("reservation_expires_at"),
                "refund_status": row.get("refund_status", "none"),
                "refund_reason": row.get("refund_reason"),
                "refund_requested_at": row.get("refund_requested_at"),
                "refund_admin_note": row.get("refund_admin_note"),
                "refund_resolved_at": row.get("refund_resolved_at"),
                "user_id": user_id_map.get(str(row.get("user_id")), row.get("user_id")),
            })

        rebuild_table(
            connection,
            "users",
            """
            CREATE TABLE users_uuid (
                id VARCHAR(36) NOT NULL,
                username VARCHAR(20),
                email VARCHAR(100),
                hashed_password VARCHAR(255),
                is_admin INTEGER,
                PRIMARY KEY (id)
            )
            """,
            """
            INSERT INTO users (id, username, email, hashed_password, is_admin)
            VALUES (:id, :username, :email, :hashed_password, :is_admin)
            """.replace("users", "users_uuid"),
            user_rows
        )

        if table_exists(connection, "weather_history"):
            rebuild_table(
                connection,
                "weather_history",
                """
                CREATE TABLE weather_history_uuid (
                    id VARCHAR(36) NOT NULL,
                    city VARCHAR(100),
                    temperature FLOAT,
                    humidity FLOAT,
                    wind_speed FLOAT,
                    user_id VARCHAR(36),
                    PRIMARY KEY (id),
                    FOREIGN KEY(user_id) REFERENCES users (id)
                )
                """,
                """
                INSERT INTO weather_history_uuid
                (id, city, temperature, humidity, wind_speed, user_id)
                VALUES (:id, :city, :temperature, :humidity, :wind_speed, :user_id)
                """,
                weather_uuid_rows
            )

        if table_exists(connection, "airports"):
            rebuild_table(
                connection,
                "airports",
                """
                CREATE TABLE airports_uuid (
                    id VARCHAR(36) NOT NULL,
                    iata_code VARCHAR(3),
                    icao_code VARCHAR(4),
                    airport_name VARCHAR(255),
                    city VARCHAR(100),
                    country VARCHAR(100),
                    latitude FLOAT,
                    longitude FLOAT,
                    PRIMARY KEY (id)
                )
                """,
                """
                INSERT INTO airports_uuid
                (id, iata_code, icao_code, airport_name, city, country, latitude, longitude)
                VALUES (:id, :iata_code, :icao_code, :airport_name, :city, :country, :latitude, :longitude)
                """,
                airport_uuid_rows
            )

        if table_exists(connection, "flight_seats"):
            rebuild_table(
                connection,
                "flight_seats",
                """
                CREATE TABLE flight_seats_uuid (
                    id VARCHAR(36) NOT NULL,
                    flight_instance_id VARCHAR(120),
                    flight_number VARCHAR(20),
                    departure_time VARCHAR(40),
                    economy_available INTEGER,
                    business_available INTEGER,
                    economy_price INTEGER,
                    business_price INTEGER,
                    PRIMARY KEY (id)
                )
                """,
                """
                INSERT INTO flight_seats_uuid
                (id, flight_instance_id, flight_number, departure_time, economy_available,
                 business_available, economy_price, business_price)
                VALUES (:id, :flight_instance_id, :flight_number, :departure_time,
                        :economy_available, :business_available, :economy_price, :business_price)
                """,
                flight_seat_uuid_rows
            )

        if table_exists(connection, "bookings"):
            rebuild_table(
                connection,
                "bookings",
                """
                CREATE TABLE bookings_uuid (
                    id VARCHAR(36) NOT NULL,
                    flight_instance_id VARCHAR(120),
                    flight_number VARCHAR(20),
                    departure_time VARCHAR(40),
                    travel_class VARCHAR(20),
                    seats INTEGER,
                    amount INTEGER,
                    payment_order_id VARCHAR(100),
                    idempotency_key VARCHAR(100),
                    status VARCHAR(20),
                    reservation_expires_at DATETIME,
                    refund_status VARCHAR(20),
                    refund_reason VARCHAR(500),
                    refund_requested_at DATETIME,
                    refund_admin_note VARCHAR(500),
                    refund_resolved_at DATETIME,
                    user_id VARCHAR(36),
                    PRIMARY KEY (id),
                    FOREIGN KEY(user_id) REFERENCES users (id)
                )
                """,
                """
                INSERT INTO bookings_uuid
                (id, flight_instance_id, flight_number, departure_time, travel_class, seats,
                 amount, payment_order_id, idempotency_key, status, reservation_expires_at,
                 refund_status, refund_reason, refund_requested_at, refund_admin_note,
                 refund_resolved_at, user_id)
                VALUES (:id, :flight_instance_id, :flight_number, :departure_time,
                        :travel_class, :seats, :amount, :payment_order_id,
                        :idempotency_key, :status, :reservation_expires_at,
                        :refund_status, :refund_reason, :refund_requested_at,
                        :refund_admin_note, :refund_resolved_at, :user_id)
                """,
                booking_uuid_rows
            )


Base.metadata.create_all(bind=engine)
migrate_existing_integer_ids_to_uuid()


def replace_unique_index_with_non_unique(index_name, table_name, columns):
    with engine.begin() as connection:
        indexes = connection.execute(text(f"PRAGMA index_list({table_name})")).fetchall()

        for index in indexes:
            if index[1] == index_name and index[2]:
                connection.execute(text(f"DROP INDEX {index_name}"))
                break

    add_index_if_missing(index_name, table_name, columns)


def backfill_flight_instances():
    with engine.begin() as connection:
        connection.execute(text(
            """
            UPDATE flight_seats
            SET flight_instance_id = flight_number || char(58) || 'unknown'
            WHERE flight_instance_id IS NULL OR flight_instance_id = ''
            """
        ))
        connection.execute(text(
            """
            UPDATE bookings
            SET flight_instance_id = flight_number || char(58) || 'unknown'
            WHERE flight_instance_id IS NULL OR flight_instance_id = ''
            """
        ))


add_column_if_missing("flight_seats", "economy_price", "economy_price INTEGER")
add_column_if_missing("flight_seats", "business_price", "business_price INTEGER")
add_column_if_missing("flight_seats", "flight_instance_id", "flight_instance_id VARCHAR(120)")
add_column_if_missing("flight_seats", "departure_time", "departure_time VARCHAR(40)")
add_column_if_missing("users", "is_admin", "is_admin INTEGER DEFAULT 0")
add_column_if_missing("bookings", "idempotency_key", "idempotency_key VARCHAR(100)")
add_column_if_missing("bookings", "payment_order_id", "payment_order_id VARCHAR(100)")
add_column_if_missing("bookings", "status", "status VARCHAR(20) DEFAULT 'confirmed'")
add_column_if_missing("bookings", "reservation_expires_at", "reservation_expires_at DATETIME")
add_column_if_missing("bookings", "flight_instance_id", "flight_instance_id VARCHAR(120)")
add_column_if_missing("bookings", "departure_time", "departure_time VARCHAR(40)")
add_column_if_missing("bookings", "refund_status", "refund_status VARCHAR(20) DEFAULT 'none'")
add_column_if_missing("bookings", "refund_reason", "refund_reason VARCHAR(500)")
add_column_if_missing("bookings", "refund_requested_at", "refund_requested_at DATETIME")
add_column_if_missing("bookings", "refund_admin_note", "refund_admin_note VARCHAR(500)")
add_column_if_missing("bookings", "refund_resolved_at", "refund_resolved_at DATETIME")
backfill_flight_instances()

add_index_if_missing("idx_airports_city", "airports", "city")
add_index_if_missing("idx_weather_history_user_city", "weather_history", "user_id, city")
add_index_if_missing("idx_bookings_user_status", "bookings", "user_id, status")
add_index_if_missing("idx_bookings_refund_status", "bookings", "refund_status")
add_index_if_missing("idx_bookings_user_refund_status", "bookings", "user_id, refund_status")
add_index_if_missing("idx_bookings_status_expiry", "bookings", "status, reservation_expires_at")
add_index_if_missing("idx_bookings_payment_order", "bookings", "payment_order_id", unique=True)
replace_unique_index_with_non_unique("ix_flight_seats_flight_number", "flight_seats", "flight_number")
add_index_if_missing("idx_flight_seats_instance", "flight_seats", "flight_instance_id", unique=True)
add_index_if_missing("idx_flight_seats_number_departure", "flight_seats", "flight_number, departure_time")
add_index_if_missing("idx_bookings_instance_class_status", "bookings", "flight_instance_id, travel_class, status")
