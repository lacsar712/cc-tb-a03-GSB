import os
import time

import psycopg2

from rules import weigh


def connect():
    last = None
    for _ in range(30):
        try:
            return psycopg2.connect(os.environ["DATABASE_URL"])
        except psycopg2.OperationalError as exc:
            last = exc
            time.sleep(1)
    raise last


def main():
    conn = connect()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS cuppings (
            id serial PRIMARY KEY,
            lot text NOT NULL,
            aroma double precision NOT NULL,
            taste double precision NOT NULL,
            liquor double precision NOT NULL,
            score double precision NOT NULL,
            verdict text NOT NULL,
            note text NOT NULL,
            created_by text NOT NULL
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS seats (
            id serial PRIMARY KEY,
            name text NOT NULL UNIQUE,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS seat_occupancies (
            id serial PRIMARY KEY,
            seat_id integer NOT NULL REFERENCES seats(id),
            occupant text NOT NULL,
            occupied_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    cur.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS one_occupant_per_seat
           ON seat_occupancies (seat_id)"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS seat_history (
            id serial PRIMARY KEY,
            seat_name text NOT NULL,
            actor text NOT NULL,
            action text NOT NULL,
            happened_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS app_settings (
            key text PRIMARY KEY,
            value text NOT NULL
        )"""
    )
    cur.execute(
        """INSERT INTO app_settings (key, value) VALUES ('seat_timeout_minutes', '30')
           ON CONFLICT (key) DO NOTHING"""
    )
    cur.execute("SELECT COUNT(*) FROM cuppings")
    if cur.fetchone()[0] == 0:
        for lot, aroma, taste, liquor in (("春茶-A", 8, 8, 7), ("夏茶-C", 5, 4, 6)):
            verdict, note, score = weigh(aroma, taste, liquor)
            cur.execute(
                """INSERT INTO cuppings (lot, aroma, taste, liquor, score, verdict, note, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (lot, aroma, taste, liquor, score, verdict, note, "taster"),
            )
    conn.commit()
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
