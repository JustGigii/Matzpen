from datetime import UTC, datetime

from sqlalchemy.dialects import oracle, sqlite
from sqlalchemy.schema import CreateTable

from personal_agent.domain.database import JSONData, UTCDateTime
from personal_agent.domain.models import Event, Person


def test_json_models_compile_for_oracle() -> None:
    event_ddl = str(CreateTable(Event.__table__).compile(dialect=oracle.dialect()))
    person_ddl = str(CreateTable(Person.__table__).compile(dialect=oracle.dialect()))

    assert "CLOB" in event_ddl
    assert "CLOB" in person_ddl
    assert "TIMESTAMP" in event_ddl


def test_json_data_round_trips_unicode() -> None:
    column_type = JSONData()
    encoded = column_type.process_bind_param({"text": "שלום"}, sqlite.dialect())

    assert encoded is not None
    assert column_type.process_result_value(encoded, sqlite.dialect()) == {"text": "שלום"}


def test_oracle_datetime_is_normalized_to_naive_utc() -> None:
    column_type = UTCDateTime()
    value = datetime(2026, 8, 4, 12, 30, tzinfo=UTC)

    stored = column_type.process_bind_param(value, oracle.dialect())
    restored = column_type.process_result_value(stored, oracle.dialect())

    assert stored == datetime(2026, 8, 4, 12, 30)
    assert restored == value
